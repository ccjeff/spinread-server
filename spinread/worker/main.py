"""Worker claim loop (LLD §5.3): `python -m spinread.worker.main`.

claim -> execute handler -> complete / fail-with-backoff. The same loop runs
inside the api process as a daemon thread when SPINREAD_EMBED_WORKER=true.
"""

from __future__ import annotations

import logging
import socket
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.config import Settings, get_settings
from spinread.core import queue
from spinread.core.db import make_engine, make_session_factory
from spinread.core.models import Job, PipelineRun, UploadSession, Video
from spinread.core.storage import S3ObjectStore, file_sha256
from spinread.pipeline import orchestrator
from spinread.pipeline.finalize import finalize_upload
from spinread.pipeline.stage import execute_stage
from spinread.pipeline.stages import REGISTRY

log = logging.getLogger(__name__)

IDLE_SLEEP_S = 2.0


def _handle_pipeline_stage(session: Session, settings: Settings, s3, job: Job) -> str:
    """Returns 'done' or 'retry'."""
    payload = job.payload
    run = session.get(PipelineRun, payload["pipeline_run_id"])
    video = session.get(Video, payload["video_id"])
    stage_impl = REGISTRY.get(payload["stage"])
    if run is None or video is None or stage_impl is None:
        log.error("PIPELINE_STAGE job %s has unknown payload %s", job.id, payload)
        return "done"
    if run.state != "RUNNING" or video.deleted_at is not None:
        return "done"

    stage_run = execute_stage(
        session, settings, s3, run, video, stage_impl, attempt=job.attempts + 1
    )
    if stage_run.status == "RETRYABLE_FAILURE":
        session.commit()
        return "retry"
    orchestrator.tick(session, run.id)
    session.commit()
    return "done"


def _handle_finalize_upload(session: Session, settings: Settings, s3, job: Job) -> str:
    upload_session = session.get(UploadSession, job.payload["upload_session_id"])
    if upload_session is None:
        return "done"
    finalize_upload(session, settings, s3, upload_session)
    session.commit()
    return "done"


def _handle_clip_render(session: Session, settings: Settings, s3, job: Job) -> str:
    """Render a CLIP/HIGHLIGHT export from the proxy via POC render_intervals."""
    import tempfile
    from pathlib import Path

    from pingpong_training.media.cliprender import render_intervals
    from pingpong_training.media.probe import FFmpegError

    from spinread.core.models import ExportManifest, MediaAsset

    manifest = session.get(ExportManifest, job.payload["export_id"])
    if manifest is None:
        return "done"
    if manifest.asset_id is not None:
        return "done"  # idempotent replay

    video = session.get(Video, manifest.video_id)
    proxy = session.scalar(
        select(MediaAsset).where(
            MediaAsset.video_id == manifest.video_id,
            MediaAsset.class_ == "PROXY",
            MediaAsset.status == "ACTIVE",
        )
    )
    if video is None or proxy is None:
        raise ValueError("export has no video/proxy")

    intervals = [(int(s), int(e)) for s, e in manifest.intervals]
    work = Path(tempfile.mkdtemp(prefix="cliprender-", dir=settings.tmp_dir))
    proxy_local = work / "proxy.mp4"
    out_local = work / "out.mp4"
    s3.download_file(proxy.object_key, str(proxy_local))

    try:
        render = render_intervals(
            proxy_local,
            intervals,
            out_local,
            pre_roll_ms=int(job.payload.get("pre_roll_ms", 0)),
            post_roll_ms=int(job.payload.get("post_roll_ms", 0)),
            reencode=True,
            ffmpeg_bin=settings.ffmpeg_bin,
            ffprobe_bin=settings.ffprobe_bin,
        )
    except FFmpegError as exc:
        raise RuntimeError(f"clip render failed: {exc}") from exc

    filename = "highlight.mp4" if manifest.kind == "HIGHLIGHT" else "clip.mp4"
    key = f"users/{video.owner_id}/videos/{video.id}/exports/{manifest.id}/{filename}"
    s3.upload_file(str(out_local), key, content_type="video/mp4")
    asset = MediaAsset(
        video_id=video.id,
        class_="CLIP" if manifest.kind == "CLIP" else "HIGHLIGHT",
        media_version=0,
        object_key=key,
        content_hash=file_sha256(str(out_local)),
        byte_size=out_local.stat().st_size,
        status="ACTIVE",
        manifest=render.model_dump(mode="json"),
    )
    session.add(asset)
    session.flush()
    manifest.asset_id = asset.id
    session.commit()
    return "done"


from spinread.product.quiz_generation import generate_quiz_candidates

HANDLERS = {
    "QUIZ_GENERATE": generate_quiz_candidates,
    "PIPELINE_STAGE": _handle_pipeline_stage,
    "FINALIZE_UPLOAD": _handle_finalize_upload,
    "CLIP_RENDER": _handle_clip_render,
    "CLEANUP": lambda session, settings, s3, job: "done",  # no-op at P0
}


def run_worker(settings: Settings | None = None, *, once: bool = False, worker_id: str | None = None) -> None:
    settings = settings or get_settings()
    worker_id = worker_id or f"{socket.gethostname()}-{id(object()):x}"
    engine = make_engine(settings.db_url)
    factory = make_session_factory(engine)
    s3 = S3ObjectStore(settings)
    log.info("worker %s started (embed=%s)", worker_id, settings.embed_worker)

    while True:
        session = factory()
        try:
            job = queue.claim(session, worker_id)
            if job is None:
                session.close()
                if once:
                    return
                time.sleep(IDLE_SLEEP_S)
                continue
            log.info("claimed job %s kind=%s", job.id, job.kind)
            handler = HANDLERS.get(job.kind)
            try:
                if handler is None:
                    raise ValueError(f"no handler for job kind {job.kind}")
                outcome = handler(session, settings, s3, job)
                if outcome == "retry":
                    queue.fail(session, job.id, "stage reported retryable failure", retryable=True)
                else:
                    queue.complete(session, job.id)
            except Exception as exc:
                session.rollback()
                log.exception("job %s failed", job.id)
                queue.fail(session, job.id, f"{type(exc).__name__}: {exc}", retryable=True)
        finally:
            session.close()
        if once:
            return


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    run_worker()


if __name__ == "__main__":
    main()
