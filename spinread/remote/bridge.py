"""Translate compute outputs into narrowly permitted application writes."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil

from sqlalchemy import select

from spinread.core.models import MediaAsset, PipelineRun, TimelineItem, Video
from spinread.core.storage import file_sha256
from spinread.pipeline.stage import StageError, StageResult
from spinread.remote.client import lease
from spinread.remote.executor import ASSET_FIELDS, COMPUTE_SETTINGS, VIDEO_FIELDS, fields
from spinread.remote.protocol import RemoteError, digest
from spinread.remote.provider import RemotePending, LeaseLost


def source(asset):
    return {"id": asset.id, "key": asset.object_key, "sha256": asset.content_hash, "bytes": asset.byte_size}


def base_payload(settings, video, asset, operation):
    return {"operation": operation, "video_id": video.id, "source": source(asset),
        "model_revision": settings.remote_model_revision,
        "settings": {k: getattr(settings, k) for k in COMPUTE_SETTINGS}}


def guard_source(db, job, payload):
    lease(db, job, lock=True)
    video = db.scalar(select(Video).where(Video.id == payload["video_id"]).with_for_update().execution_options(populate_existing=True))
    asset = db.get(MediaAsset, payload["source"]["id"], populate_existing=True)
    if not video or video.deleted_at or not asset or asset.status != "ACTIVE" or source(asset) != payload["source"]:
        raise RemoteError("SOURCE_CHANGED")
    return video


class RemoteStage:
    def __init__(self, implementation, client, job):
        self.original, self.client, self.job = implementation, client, job
        self.stage, self.stage_version = implementation.stage, implementation.stage_version

    def cache_fingerprint(self, settings):
        original = getattr(self.original, "cache_fingerprint", lambda _: "")(settings)
        return digest({"original": original, "remote_model_revision": settings.remote_model_revision,
                       "remote_vision_enabled": settings.remote_vision_enabled})

    def run(self, ctx):
        db = ctx.session
        original = ctx.active_original()
        assets = db.scalars(select(MediaAsset).where(MediaAsset.video_id == ctx.video.id,
            MediaAsset.status == "ACTIVE").order_by(MediaAsset.id)).all()
        snapshot = [fields(a, ASSET_FIELDS) for a in assets]
        payload = {**base_payload(ctx.settings, ctx.video, original, "stage"),
            "stage": self.stage, "stage_version": self.stage_version,
            "video": fields(ctx.video, VIDEO_FIELDS),
            "run": fields(ctx.pipeline_run, ("id", "video_id", "pipeline_version", "trigger", "state", "scope")),
            "stage_run": fields(ctx.stage_run, ("id", "pipeline_run_id", "stage", "stage_version", "idempotency_key")),
            "assets": snapshot,
            "prior": {name: {**fields(a, ("id", "video_id", "pipeline_run_id", "stage", "stage_version", "object_key", "content_hash")),
                "payload": ctx.load_artifact_json(a)} for name, a in ctx.prior_artifacts.items()}}
        # PROBE's persisted output changes video metadata; retries after remote
        # completion but before publication still use the same input snapshot.
        root = ctx.work_dir / "remote"
        try:
            result, files = self.client.run(db, self.job, payload, root,
                gpu=self.stage in {"ACTIVITY", "RALLY", "EVENTS"} and ctx.settings.remote_vision_enabled)
            guard_source(db, self.job, payload)
            run = db.get(PipelineRun, ctx.pipeline_run.id, populate_existing=True)
            if run.state != "RUNNING":
                raise RemoteError("PIPELINE_CANCELLED")
            if self.stage == "NORMALIZE":
                current = db.scalars(select(MediaAsset).where(MediaAsset.video_id == ctx.video.id,
                    MediaAsset.status == "ACTIVE").order_by(MediaAsset.id)).all()
                if [fields(a, ASSET_FIELDS) for a in current] != snapshot:
                    raise RemoteError("MEDIA_VERSION_CHANGED")
                self._publish_media(ctx, snapshot, result, files)
            elif files or result["assets"]:
                raise RemoteError("UNEXPECTED_STAGE_FILES")
            if self.stage == "PROBE":
                ctx.video.probe = result["video"]["probe"]
                ctx.video.duration_ms = result["video"]["duration_ms"]
            value = StageResult(**result["stage_result"])
            if value.status not in {"SUCCEEDED", "PARTIAL_SUCCESS", "SKIPPED_UNSUPPORTED"}:
                raise RemoteError("INVALID_STAGE_STATUS")
            if value.artifact_name is not None and not re.fullmatch(r"[a-z0-9_-]+\.json", value.artifact_name):
                raise RemoteError("INVALID_ARTIFACT_NAME")
            return value
        except (RemotePending, LeaseLost):
            raise
        except RemoteError as exc:
            raise StageError(str(exc), str(exc)) from None
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def _publish_media(self, ctx, snapshot, result, files):
        derived = [a for a in snapshot if a["class_"] in {"PROXY", "STREAM", "THUMBNAIL"}]
        version = 1 + max((a["media_version"] for a in derived), default=0)
        prefix = f"users/{ctx.video.owner_id}/videos/{ctx.video.id}/derived/{version}/"
        def valid_key(key):
            return key.startswith(prefix) and re.fullmatch(r"(proxy\.mp4|stream/(master\.m3u8|seg_[0-9]+\.ts)|thumbs/[0-9]+\.jpg)", key[len(prefix):])
        if any(not valid_key(k) for k in files):
            raise RemoteError("INVALID_MEDIA_KEY")
        classes = set()
        new_assets = []
        for spec in result["assets"]:
            key = spec["object_key"]
            if (spec["video_id"] != ctx.video.id or spec["class_"] not in {"PROXY", "STREAM", "THUMBNAIL"}
                    or spec["media_version"] != version or spec["status"] != "ACTIVE" or key not in files):
                raise RemoteError("INVALID_MEDIA_ASSET")
            path = files[key]
            if spec["byte_size"] != path.stat().st_size or spec["content_hash"] != file_sha256(str(path)):
                raise RemoteError("MEDIA_HASH_MISMATCH")
            if spec["class_"] == "STREAM":
                segments = spec["manifest"]["segments"]
                if any(not re.fullmatch(r"seg_[0-9]+\.ts", s) or prefix + "stream/" + s not in files for s in segments):
                    raise RemoteError("INVALID_HLS_SEGMENTS")
                playlist = path.read_text()
                if any(line and not line.startswith("#") and line not in segments for line in playlist.splitlines()):
                    raise RemoteError("INVALID_HLS_PLAYLIST")
            classes.add(spec["class_"])
            new_assets.append(MediaAsset(**{k: v for k, v in spec.items() if k != "id"}))
        if not {"PROXY", "STREAM"}.issubset(classes):
            raise RemoteError("MISSING_MEDIA_OUTPUTS")
        for key, path in files.items():
            ctx.s3.upload_file(str(path), key)
        for spec in derived:
            ctx.session.get(MediaAsset, spec["id"]).status = "RETIRED"
        ctx.session.add_all(new_assets)


def replay(db, settings, analysis, video, original, root):
    client, job = db.info["remote_client"], db.info["dispatcher_job"]
    rows = db.scalars(select(TimelineItem).where(TimelineItem.timeline_id == analysis.timeline_id,
        TimelineItem.type == "RALLY", TimelineItem.status == "ACTIVE").order_by(TimelineItem.start_ms)).all()
    payload = {**base_payload(settings, video, original, "replay"), "engine": analysis.engine,
        "analysis_id": analysis.id, "timeline_id": analysis.timeline_id, "timeline_version": analysis.timeline_version,
        "duration_ms": video.duration_ms, "players": analysis.options.get("players", []),
        "rallies": [{"start_ms": r.start_ms, "end_ms": r.end_ms} for r in rows]}
    result, files = client.run(db, job, payload, root / "remote", gpu=True)
    guard_source(db, job, payload)
    for key, path in files.items():
        if not re.fullmatch(r"mesh/[A-Za-z0-9_-]{1,128}\.(bin|json)", key):
            raise RemoteError("INVALID_MESH_PATH")
        dest = root / "nlf" / key.removeprefix("mesh/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
    return result["evidence"], result["failures"], result["mesh_manifest"]


def clip(db, settings, video, proxy, manifest, job, root):
    payload = {**base_payload(settings, video, proxy, "clip"), "intervals": manifest.intervals,
        "pre_roll_ms": int(job.payload.get("pre_roll_ms", 0)), "post_roll_ms": int(job.payload.get("post_roll_ms", 0))}
    result, files = db.info["remote_client"].run(db, job, payload, root / "remote")
    guard_source(db, job, payload)
    if set(files) != {"clip.mp4"}:
        raise RemoteError("INVALID_CLIP_OUTPUT")
    shutil.copyfile(files["clip.mp4"], root / "out.mp4")
    return result["render"]


def quiz(db, settings, video, original, generation, root):
    from pingpong_training.analysis.serve import ServeCandidate
    job = db.info["dispatcher_job"]
    payload = {**base_payload(settings, video, original, "quiz"), "generation_id": generation.id,
               "timeline_id": generation.timeline_id, "duration_ms": video.duration_ms}
    result, files = db.info["remote_client"].run(db, job, payload, root / "remote",
                                              gpu=settings.remote_vision_enabled)
    guard_source(db, job, payload)
    if files:
        raise RemoteError("UNEXPECTED_QUIZ_FILES")
    return [ServeCandidate(**c) for c in result["candidates"]], result["windows"], result["impacts"]
