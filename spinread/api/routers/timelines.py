"""Timeline edits (HLD §8.6) + arbitrary-version timeline reads."""

from __future__ import annotations
from spinread.core.access import viewable_video

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.api.deps import get_current_user, get_db, get_owned_video
from spinread.api.errors import ApiError, error_body, not_found
from spinread.api.schemas import ActiveTimelineOut, TimelineItemOut
from spinread.core.models import (
    Timeline,
    TimelineActivePointer,
    TimelineItem,
    User,
    Video,
)
from spinread.product.timeline import (
    EditError,
    VersionConflict,
    apply_timeline_edits,
)

router = APIRouter(prefix="/api/videos", tags=["timelines"])


class UpdateBoundaryOp(BaseModel):
    op: Literal["UPDATE_BOUNDARY"]
    timeline_item_id: str
    start_ms: int
    end_ms: int


class SetLabelOp(BaseModel):
    op: Literal["SET_LABEL"]
    timeline_item_id: str
    field: Literal["type"]
    value: str


class SplitOp(BaseModel):
    op: Literal["SPLIT"]
    timeline_item_id: str
    at_ms: int


class MergeNextOp(BaseModel):
    op: Literal["MERGE_NEXT"]
    timeline_item_id: str


class DeleteOp(BaseModel):
    op: Literal["DELETE"]
    timeline_item_id: str


class TimelineEditsRequest(BaseModel):
    base_timeline_version: int
    operations: list[UpdateBoundaryOp | SetLabelOp | SplitOp | MergeNextOp | DeleteOp]


class TimelineEditsResponse(BaseModel):
    timeline_id: str
    version: int
    n_items: int


@router.post("/{video_id}/timeline-edits", response_model=TimelineEditsResponse)
def post_timeline_edits(
    video_id: str,
    body: TimelineEditsRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TimelineEditsResponse:
    video = get_owned_video(video_id, db, user)
    try:
        timeline, n_items = apply_timeline_edits(
            db,
            video,
            body.base_timeline_version,
            [op.model_dump() for op in body.operations],
        )
    except VersionConflict as conflict:
        raise ApiError(
            409,
            "TIMELINE_VERSION_CONFLICT",
            "base_timeline_version does not match the active timeline",
            {
                "current": {
                    "version": conflict.current.version,
                    "timeline_id": conflict.current.id,
                }
            },
        )
    except EditError as exc:
        raise ApiError(422, exc.code, exc.message, exc.details)
    return TimelineEditsResponse(
        timeline_id=timeline.id, version=timeline.version, n_items=n_items
    )


@router.get("/{video_id}/timelines/{version}", response_model=ActiveTimelineOut)
def get_timeline_version(
    video_id: str,
    version: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ActiveTimelineOut:
    video = viewable_video(video_id, db, user)
    timeline = db.scalar(
        select(Timeline).where(
            Timeline.video_id == video.id, Timeline.version == version
        )
    )
    if timeline is None:
        raise not_found("timeline version not found")
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
