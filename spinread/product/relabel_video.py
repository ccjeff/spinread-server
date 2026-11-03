"""Rebuild activity, rallies and hits while retaining media and timeline history."""
from sqlalchemy import select
from spinread.core.models import (
    Artifact, MediaAsset, PipelineRun, StageRun, Timeline,
    TimelineActivePointer, Video, utcnow,
)
from spinread.pipeline import orchestrator
from spinread.pipeline.dag import PIPELINE_VERSION
from spinread.product.timeline import EditError, VersionConflict

REUSED_STAGES = ("PROBE", "NORMALIZE", "QUALITY")


def create_relabel_run(db, video_id: str, base_version: int):
    """Caller owns transaction. Existing published timeline stays live until TIMELINE."""
    video = db.scalar(select(Video).where(Video.id == video_id).with_for_update())
    if video is None or video.deleted_at:
        raise EditError("VIDEO_UNAVAILABLE", "Video unavailable")
    if db.scalar(select(PipelineRun.id).where(
        PipelineRun.video_id == video_id, PipelineRun.state == "RUNNING"
    )):
        raise EditError("RUN_ACTIVE", "Analysis in progress")
    pointer = db.get(TimelineActivePointer, video_id)
    if pointer is None:
        raise EditError("NO_TIMELINE", "No timeline")
    previous = db.get(Timeline, pointer.timeline_id)
    if previous.version != base_version:
        raise VersionConflict(previous)
    original = db.scalar(select(MediaAsset).where(
        MediaAsset.video_id == video_id, MediaAsset.class_ == "ORIGINAL",
        MediaAsset.status == "ACTIVE",
    ))
    if original is None:
        raise EditError("NO_ORIGINAL", "No active original")

    reused = []
    for stage in REUSED_STAGES:
        candidates = db.scalars(select(StageRun).join(
            PipelineRun, PipelineRun.id == StageRun.pipeline_run_id
        ).where(
            PipelineRun.video_id == video_id, StageRun.stage == stage,
            StageRun.status.in_(("SUCCEEDED", "REUSED_CACHE")),
            StageRun.output_artifact_id.is_not(None),
        ).order_by(StageRun.finished_at.desc())).all()
        source = next((s for s in candidates if original.id in s.input_artifact_ids
                       and db.get(Artifact, s.output_artifact_id) is not None), None)
        if source is None:
            raise EditError("MISSING_MEDIA_ANALYSIS", f"No reusable {stage}; run full pipeline")
        reused.append(source)

    run = PipelineRun(video_id=video_id, pipeline_version=PIPELINE_VERSION, trigger="RERUN")
    db.add(run)
    db.flush()
    for source in reused:
        db.add(StageRun(
            pipeline_run_id=run.id, stage=source.stage, stage_version=source.stage_version,
            idempotency_key=source.idempotency_key, status="REUSED_CACHE", attempt=0,
            input_artifact_ids=list(source.input_artifact_ids),
            output_artifact_id=source.output_artifact_id,
            started_at=utcnow(), finished_at=utcnow(),
            metrics={**(source.metrics or {}), "reused_from_stage_run": source.id},
        ))
    db.flush()
    orchestrator.tick(db, run.id)
    return run
