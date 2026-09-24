"""Stage execution framework (LLD §5.2/§5.4).

A Stage implementation computes a StageResult from a StageContext. The
framework handles idempotency-key computation, cache reuse, artifact
publication (tmp object -> checksum -> copy to final key -> DB rows in one
transaction) and stage_run bookkeeping.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.config import Settings
from spinread.core.models import Artifact, PipelineRun, StageRun, Video, utcnow
from spinread.core.storage import analysis_key

log = logging.getLogger(__name__)


class StageError(Exception):
    """Permanent stage failure (invalid input, deterministic). retryable=False."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class RetryableStageError(Exception):
    """Transient failure; the job layer retries with backoff."""

    def __init__(self, message: str, code: str = "TRANSIENT_ERROR"):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class StageContext:
    session: Session
    settings: Settings
    s3: "object"  # S3ObjectStore
    video: Video
    pipeline_run: PipelineRun
    stage_run: StageRun
    work_dir: Path
    prior_artifacts: dict[str, Artifact] = field(default_factory=dict)

    def load_artifact_json(self, artifact: Artifact) -> dict:
        return json.loads(self.s3.get_bytes(artifact.object_key).decode("utf-8"))

    def active_original(self) -> "MediaAsset":
        from spinread.core.models import MediaAsset

        original = self.session.scalar(
            select(MediaAsset).where(
                MediaAsset.video_id == self.video.id,
                MediaAsset.class_ == "ORIGINAL",
                MediaAsset.status == "ACTIVE",
            )
        )
        if original is None:
            raise StageError("NO_ORIGINAL", "video has no ACTIVE ORIGINAL media asset")
        return original

    def ensure_original_local(self) -> tuple[Path, Path]:
        """Download ORIGINAL to the stable cross-run cache path.

        Returns (local_video_path, poc_work_dir). The work dir is shared by
        POC caches keyed on content (pcm.npy, gray_frames.npy), so repeated
        runs of ACTIVITY/RALLY on the same media skip the re-decode.
        """
        original = self.active_original()
        cache_root = Path(self.settings.tmp_dir) / "cache" / original.content_hash
        cache_root.mkdir(parents=True, exist_ok=True)
        local = cache_root / "original"
        if not local.exists():
            tmp = cache_root / f".download-{self.stage_run.id}"
            self.s3.download_file(original.object_key, str(tmp))
            tmp.replace(local)  # atomic publish; concurrent downloads race safely
        work = cache_root / "work"
        work.mkdir(exist_ok=True)
        return local, work


@dataclass
class StageResult:
    """What a stage produced. artifact=None means no artifact row (e.g. TIMELINE)."""

    artifact_name: str | None = None  # e.g. "probe.json"
    artifact_json: dict | None = None
    metrics: dict | None = None
    limitations: list[str] | None = None
    status: str = "SUCCEEDED"  # SUCCEEDED | PARTIAL_SUCCESS | SKIPPED_UNSUPPORTED


class Stage(Protocol):
    stage: str
    stage_version: str

    def run(self, ctx: StageContext) -> StageResult: ...


def compute_idempotency_key(
    video_id: str, stage: str, stage_version: str, input_hashes: list[str]
) -> str:
    h = hashlib.sha256()
    h.update(video_id.encode())
    h.update(b"|")
    h.update(stage.encode())
    h.update(b"|")
    h.update(stage_version.encode())
    for ih in sorted(input_hashes):
        h.update(b"|")
        h.update(ih.encode())
    return h.hexdigest()


def gather_prior_artifacts(
    session: Session, pipeline_run_id: str
) -> dict[str, Artifact]:
    """Latest artifact per stage referenced by this run's stage_runs.

    Goes through stage_runs.output_artifact_id (not artifacts.pipeline_run_id)
    so artifacts reused via content-hash dedupe are visible to later stages.
    """
    rows = session.scalars(
        select(Artifact)
        .join(StageRun, StageRun.output_artifact_id == Artifact.id)
        .where(StageRun.pipeline_run_id == pipeline_run_id)
        .order_by(Artifact.created_at)
    ).all()
    return {a.stage: a for a in rows}


def _gather_inputs(
    session: Session, pipeline_run_id: str, video: Video
) -> tuple[list[str], list[str], dict[str, Artifact]]:
    """(input_content_hashes, input_artifact_or_asset_ids, prior_artifacts)."""
    from spinread.core.models import MediaAsset

    prior = gather_prior_artifacts(session, pipeline_run_id)
    input_hashes: list[str] = []
    input_ids: list[str] = []
    original = session.scalar(
        select(MediaAsset).where(
            MediaAsset.video_id == video.id,
            MediaAsset.class_ == "ORIGINAL",
            MediaAsset.status == "ACTIVE",
        )
    )
    if original is not None:
        input_hashes.append(original.content_hash)
        input_ids.append(original.id)
    for art in prior.values():
        input_hashes.append(art.content_hash)
        input_ids.append(art.id)
    return input_hashes, input_ids, prior


def execute_stage(
    session: Session,
    settings: Settings,
    s3,
    pipeline_run: PipelineRun,
    video: Video,
    stage_impl: Stage,
    attempt: int = 1,
) -> StageRun:
    """Run one stage with idempotency + artifact publication. Returns stage_run.

    StageError / RetryableStageError raised by the implementation are caught
    and recorded on the stage_run (PERMANENT_FAILURE / RETRYABLE_FAILURE);
    the caller inspects stage_run.status to decide job retry. Unexpected
    exceptions propagate (the caller rolls back and retries the job).
    """
    input_hashes, input_ids, prior = _gather_inputs(session, pipeline_run.id, video)

    fingerprint = getattr(stage_impl, "cache_fingerprint", None)
    if fingerprint is not None:
        input_hashes.append(fingerprint(settings))
    idem_key = compute_idempotency_key(
        video.id, stage_impl.stage, stage_impl.stage_version, input_hashes
    )

    existing = session.scalar(
        select(StageRun).where(
            StageRun.pipeline_run_id == pipeline_run.id,
            StageRun.stage == stage_impl.stage,
            StageRun.idempotency_key == idem_key,
            StageRun.status.in_(["SUCCEEDED", "PARTIAL_SUCCESS", "SKIPPED_UNSUPPORTED"]),
        )
    )
    if existing is not None:
        cached = StageRun(
            pipeline_run_id=pipeline_run.id,
            stage=stage_impl.stage,
            stage_version=stage_impl.stage_version,
            idempotency_key=idem_key,
            status="REUSED_CACHE",
            attempt=existing.attempt,
            input_artifact_ids=input_ids,
            output_artifact_id=existing.output_artifact_id,
            started_at=utcnow(),
            finished_at=utcnow(),
            metrics=existing.metrics,
        )
        session.add(cached)
        session.flush()
        return cached

    stage_run = StageRun(
        pipeline_run_id=pipeline_run.id,
        stage=stage_impl.stage,
        stage_version=stage_impl.stage_version,
        idempotency_key=idem_key,
        status="RUNNING",
        attempt=attempt,
        input_artifact_ids=input_ids,
        started_at=utcnow(),
    )
    session.add(stage_run)
    session.flush()

    work_dir = Path(settings.tmp_dir) / pipeline_run.id / stage_impl.stage.lower()
    work_dir.mkdir(parents=True, exist_ok=True)
    ctx = StageContext(
        session=session,
        settings=settings,
        s3=s3,
        video=video,
        pipeline_run=pipeline_run,
        stage_run=stage_run,
        work_dir=work_dir,
        prior_artifacts=prior,
    )

    try:
        result = stage_impl.run(ctx)
    except (StageError, RetryableStageError) as exc:
        stage_run.status = (
            "PERMANENT_FAILURE" if isinstance(exc, StageError) else "RETRYABLE_FAILURE"
        )
        stage_run.error_code = getattr(exc, "code", "STAGE_ERROR")
        stage_run.error_message = str(exc)[:1000]
        stage_run.finished_at = utcnow()
        session.flush()
        return stage_run

    artifact_id = None
    if result.artifact_name is not None and result.artifact_json is not None:
        payload = json.dumps(result.artifact_json, indent=1, sort_keys=True).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        # artifacts.content_hash is globally UNIQUE (LLD §14.2 dedupe): when an
        # identical artifact already exists (e.g. a rerun reproduces the same
        # bytes), reference it instead of inserting a duplicate row.
        existing_artifact = session.scalar(
            select(Artifact).where(Artifact.content_hash == digest)
        )
        if existing_artifact is not None:
            artifact_id = existing_artifact.id
        else:
            owner_id = video.owner_id
            tmp_key = analysis_key(
                owner_id, video.id, pipeline_run.id, stage_impl.stage,
                f"tmp/{stage_run.id}.json",
            )
            final_key = analysis_key(
                owner_id, video.id, pipeline_run.id, stage_impl.stage, result.artifact_name
            )
            s3.put_bytes(tmp_key, payload, content_type="application/json")
            head = s3.head(tmp_key)
            if head is None or int(head.get("ContentLength", -1)) != len(payload):
                raise RetryableStageError("artifact tmp write could not be verified")
            s3.copy(tmp_key, final_key)
            s3.delete(tmp_key)

            artifact = Artifact(
                video_id=video.id,
                pipeline_run_id=pipeline_run.id,
                stage=stage_impl.stage,
                stage_version=stage_impl.stage_version,
                object_key=final_key,
                content_hash=digest,
                metrics=result.metrics,
                limitations=result.limitations,
            )
            session.add(artifact)
            session.flush()
            artifact_id = artifact.id

    stage_run.status = result.status
    stage_run.output_artifact_id = artifact_id
    stage_run.metrics = result.metrics
    stage_run.finished_at = utcnow()
    session.flush()
    return stage_run
