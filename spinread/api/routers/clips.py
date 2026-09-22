"""Clip / highlight-reel export API (HLD §9.2)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.api.deps import get_current_user, get_db, get_owned_video, get_s3
from spinread.api.errors import bad_request, not_found
from spinread.core.models import ExportManifest, MediaAsset, TimelineItem, User
from spinread.core.storage import S3ObjectStore
from spinread.product.exports import (
    CLIP_PRESET,
    DEFAULT_POST_ROLL_MS,
    DEFAULT_PRE_ROLL_MS,
    HIGHLIGHT_PRESET,
    ExportError,
    create_export,
    export_status,
)

router = APIRouter(prefix="/api", tags=["exports"])


class CreateClipRequest(BaseModel):
    video_id: str
    timeline_item_id: str
    pre_roll_ms: int = Field(default=DEFAULT_PRE_ROLL_MS, ge=0, le=10_000)
    post_roll_ms: int = Field(default=DEFAULT_POST_ROLL_MS, ge=0, le=10_000)


class CreateHighlightRequest(BaseModel):
    video_id: str
    timeline_item_ids: list[str] = Field(min_length=1)


class ExportOut(BaseModel):
    clip_id: str
    video_id: str
    kind: str
    status: str
    intervals: list
    download_url: str | None = None


def _out(db: Session, m: ExportManifest) -> ExportOut:
    status = export_status(db, m)
    return ExportOut(
        clip_id=m.id,
        video_id=m.video_id,
        kind=m.kind,
        status=status,
        intervals=m.intervals,
        download_url=f"/api/clips/{m.id}/download" if status == "READY" else None,
    )


def _item_interval(db: Session, video_id: str, item_id: str) -> tuple[int, int]:
    item = db.get(TimelineItem, item_id)
    if item is None:
        raise bad_request("ITEM_NOT_FOUND", f"timeline item {item_id} not found")
    from spinread.core.models import Timeline

    timeline = db.get(Timeline, item.timeline_id)
    if timeline is None or timeline.video_id != video_id:
        raise bad_request("ITEM_NOT_FOUND", "timeline item does not belong to this video")
    return item.start_ms, item.end_ms


@router.post("/clips", response_model=ExportOut, status_code=201)
def create_clip(
    body: CreateClipRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExportOut:
    video = get_owned_video(body.video_id, db, user)
    interval = _item_interval(db, video.id, body.timeline_item_id)
    try:
        manifest, _created = create_export(
            db, video, "CLIP", [interval], CLIP_PRESET,
            body.pre_roll_ms, body.post_roll_ms,
        )
    except ExportError as exc:
        raise bad_request(exc.code, exc.message)
    return _out(db, manifest)


@router.get("/clips", response_model=list[ExportOut])
def list_clips(
    video_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ExportOut]:
    video = get_owned_video(video_id, db, user)
    rows = db.scalars(
        select(ExportManifest)
        .where(ExportManifest.video_id == video.id, ExportManifest.kind == "CLIP")
        .order_by(ExportManifest.created_at.desc())
    ).all()
    return [_out(db, m) for m in rows]


def _get_owned_export(db: Session, export_id: str, user: User, kind: str | None = None) -> ExportManifest:
    manifest = db.get(ExportManifest, export_id)
    if manifest is None or (kind is not None and manifest.kind != kind):
        raise not_found("export not found")
    get_owned_video(manifest.video_id, db, user)
    return manifest


@router.get("/clips/{clip_id}", response_model=ExportOut)
def get_clip(
    clip_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExportOut:
    manifest = _get_owned_export(db, clip_id, user)
    return _out(db, manifest)


@router.get("/clips/{clip_id}/download")
def download_clip(
    clip_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    s3: S3ObjectStore = Depends(get_s3),
) -> Response:
    manifest = _get_owned_export(db, clip_id, user)
    if manifest.asset_id is None:
        raise not_found("clip is not rendered yet")
    asset = db.get(MediaAsset, manifest.asset_id)
    if asset is None or asset.status != "ACTIVE":
        raise not_found("clip asset unavailable")
    return Response(
        content=s3.get_bytes(asset.object_key),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{manifest.id}.mp4"'},
    )


@router.post("/highlight-reels", response_model=ExportOut, status_code=201)
def create_highlight(
    body: CreateHighlightRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExportOut:
    video = get_owned_video(body.video_id, db, user)
    intervals = [_item_interval(db, video.id, iid) for iid in body.timeline_item_ids]
    try:
        manifest, _created = create_export(
            db, video, "HIGHLIGHT", intervals, HIGHLIGHT_PRESET,
            DEFAULT_PRE_ROLL_MS, DEFAULT_POST_ROLL_MS,
        )
    except ExportError as exc:
        raise bad_request(exc.code, exc.message)
    return _out(db, manifest)


@router.get("/highlight-reels/{reel_id}", response_model=ExportOut)
def get_highlight(
    reel_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExportOut:
    manifest = _get_owned_export(db, reel_id, user, kind="HIGHLIGHT")
    return _out(db, manifest)


@router.get("/highlight-reels/{reel_id}/download")
def download_highlight(
    reel_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    s3: S3ObjectStore = Depends(get_s3),
) -> Response:
    return download_clip(reel_id, db, user, s3)
