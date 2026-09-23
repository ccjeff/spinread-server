"""Videos: list, detail, soft-delete, processing status, active timeline."""

from __future__ import annotations
from spinread.core.access import viewable_video, audit

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.api.deps import get_current_user, get_db, get_owned_video
from spinread.api.errors import not_found
from spinread.api.schemas import (
    ActiveTimelineOut,
    ProcessingStatusOut,
    StageStatus,
    TimelineItemOut,
    VideoListItem,
    VideoOut,
)
from spinread.core.models import (
    MediaAsset,
    Timeline,
    TimelineActivePointer,
    TimelineItem,
    User,
    Video,
    utcnow,
)
from spinread.pipeline.dag import STAGES
from spinread.pipeline.orchestrator import progress

router = APIRouter(prefix="/api/videos", tags=["videos"])


def _list_item(v: Video) -> VideoListItem:
    return VideoListItem(
        id=v.id,
        state=v.state,
        filename=v.filename,
        session_type=v.session_type,
        duration_ms=v.duration_ms,
        created_at=v.created_at,
    )


@router.get("", response_model=list[VideoListItem])
def list_videos(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[VideoListItem]:
    rows = db.scalars(
        select(Video)
        .where(Video.owner_id == user.id, Video.deleted_at.is_(None))
        .order_by(Video.created_at.desc())
    ).all()
    return [_list_item(v) for v in rows]


@router.get("/{video_id}", response_model=VideoOut)
def get_video(
    video_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> VideoOut:
    video = viewable_video(video_id, db, user)
    return VideoOut(
        **_list_item(video).model_dump(),
        owner_id=video.owner_id,
        target_player=video.target_player,
        recorded_at=video.recorded_at,
        probe=video.probe,
    )


@router.delete("/{video_id}", status_code=204)
def delete_video(
    video_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> None:
    video = get_owned_video(video_id, db, user)
    audit(db, user.id, "VIDEO_DELETE_REQUESTED", video.id, video.id)
    video.deleted_at = utcnow()
    video.state = "DELETED"
    assets = db.scalars(select(MediaAsset).where(MediaAsset.video_id == video.id)).all()
    for asset in assets:
        if asset.status == "ACTIVE":
            asset.status = "PURGE_SCHEDULED"
    # Physical removal is intentionally not implemented (CLEANUP job seam).
    return None


@router.post("/{video_id}/pipeline-runs", status_code=201)
def create_pipeline_run(
    video_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Manual full rerun (LLD §14.3 manual retry). 409 when one is RUNNING."""
    from spinread.core.models import MediaAsset, PipelineRun
    from spinread.pipeline.dag import PIPELINE_VERSION
    from spinread.pipeline import orchestrator
    from spinread.api.errors import ApiError

    video = get_owned_video(video_id, db, user)
    running = db.scalar(
        select(PipelineRun).where(
            PipelineRun.video_id == video.id, PipelineRun.state == "RUNNING"
        )
    )
    if running is not None:
        raise ApiError(
            409,
            "PIPELINE_RUN_ACTIVE",
            "a pipeline run is already in progress",
            {"pipeline_run_id": running.id},
        )
    original = db.scalar(
        select(MediaAsset).where(
            MediaAsset.video_id == video.id,
            MediaAsset.class_ == "ORIGINAL",
            MediaAsset.status == "ACTIVE",
        )
    )
    if original is None:
        raise not_found("video has no media to process")

    run = PipelineRun(
        video_id=video.id, pipeline_version=PIPELINE_VERSION, trigger="MANUAL_RERUN"
    )
    db.add(run)
    db.flush()
    video.state = "PROBING"
    orchestrator.tick(db, run.id)
    db.flush()
    return {"pipeline_run_id": run.id, "trigger": run.trigger, "state": run.state}


@router.get("/{video_id}/processing-status", response_model=ProcessingStatusOut)
def processing_status(
    video_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ProcessingStatusOut:
    video = get_owned_video(video_id, db, user)
    run, statuses, pct = progress(db, video.id)

    limitations: list[str] = []
    seen: set[str] = set()
    for art_stage, art in sorted(
        ((a.stage, a) for a in _artifacts(db, video.id)), key=lambda t: t[0]
    ):
        for lim in art.limitations or []:
            if lim not in seen:
                seen.add(lim)
                limitations.append(lim)

    stages: list[StageStatus] = []
    stage_names = list(run.scope) if (run is not None and run.scope) else list(STAGES)
    for stage in stage_names:
        sr = statuses.get(stage)
        if sr is None:
            stages.append(StageStatus(stage=stage, status="PENDING", attempt=0))
        else:
            stages.append(
                StageStatus(
                    stage=stage,
                    status=sr.status,
                    attempt=sr.attempt,
                    error_code=sr.error_code,
                )
            )
    state = video.state if run is not None else video.state
    return ProcessingStatusOut(
        state=state, progress_pct=pct, stages=stages, limitations=limitations
    )


def _artifacts(db: Session, video_id: str):
    from spinread.core.models import Artifact

    return db.scalars(
        select(Artifact).where(Artifact.video_id == video_id).order_by(Artifact.created_at)
    ).all()


@router.get("/{video_id}/timelines/active", response_model=ActiveTimelineOut)
def active_timeline(
    video_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ActiveTimelineOut:
    video = viewable_video(video_id, db, user)
    pointer = db.get(TimelineActivePointer, video.id)
    if pointer is None:
        raise not_found("no active timeline for this video")
    timeline = db.get(Timeline, pointer.timeline_id)
    if timeline is None:
        raise not_found("active timeline missing")
    items = db.scalars(
        select(TimelineItem)
        .where(TimelineItem.timeline_id == timeline.id, TimelineItem.status == "ACTIVE")
        .order_by(TimelineItem.start_ms)
    ).all()
    return ActiveTimelineOut(
        timeline_id=timeline.id,
        version=timeline.version,
        video_duration_ms=video.duration_ms,
        items=[
            TimelineItemOut(
                item_id=i.id,
                parent_id=i.parent_id,
                type=i.type,
                start_ms=i.start_ms,
                end_ms=i.end_ms,
                actor=i.actor,
                confidence=i.confidence,
                provenance=i.provenance,
                attributes=i.attributes,
            )
            for i in items
        ],
    )
