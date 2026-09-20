"""Media gateway (HLD §6.3 "authorized media gateway"): authorized, no presigned GETs.

- GET /api/videos/{id}/stream/master.m3u8  — playlist with segment URIs rewritten
- GET /api/videos/{id}/stream/{segment}.ts — segment bytes
- GET /api/videos/{id}/media/proxy         — proxy.mp4 with Range support (206)
- GET /api/videos/{id}/thumbs/{ts_ms}.jpg  — thumbnails
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.api.deps import get_current_user, get_db, get_owned_video, get_s3
from spinread.api.errors import bad_request, not_found
from spinread.core.models import MediaAsset, User
from spinread.core.storage import S3ObjectStore

router = APIRouter(prefix="/api/videos", tags=["media"])


def _active_asset(db: Session, video_id: str, class_: str, object_key: str | None = None) -> MediaAsset:
    q = select(MediaAsset).where(
        MediaAsset.video_id == video_id,
        MediaAsset.class_ == class_,
        MediaAsset.status == "ACTIVE",
    )
    if object_key is not None:
        q = q.where(MediaAsset.object_key == object_key)
    asset = db.scalar(q)
    if asset is None:
        raise not_found("media not ready")
    return asset


@router.get("/{video_id}/stream/master.m3u8")
def get_master_playlist(
    video_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    s3: S3ObjectStore = Depends(get_s3),
) -> Response:
    video = get_owned_video(video_id, db, user)
    asset = _active_asset(db, video.id, "STREAM")
    playlist = s3.get_bytes(asset.object_key).decode("utf-8")
    base = f"/api/videos/{video.id}/stream"
    out_lines = []
    for line in playlist.splitlines():
        if line and not line.startswith("#"):
            line = f"{base}/{line.strip()}"
        out_lines.append(line)
    return Response(
        content="\n".join(out_lines) + "\n",
        media_type="application/vnd.apple.mpegurl",
    )


@router.get("/{video_id}/stream/{segment}")
def get_segment(
    video_id: str,
    segment: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    s3: S3ObjectStore = Depends(get_s3),
) -> Response:
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.ts", segment):
        raise bad_request("BAD_SEGMENT", "invalid segment name")
    video = get_owned_video(video_id, db, user)
    stream = _active_asset(db, video.id, "STREAM")
    key = stream.object_key.rsplit("/", 1)[0] + "/" + segment
    head = s3.head(key)
    if head is None:
        raise not_found("segment not found")
    return Response(content=s3.get_bytes(key), media_type="video/mp2t")


@router.get("/{video_id}/media/proxy")
def get_proxy(
    video_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    s3: S3ObjectStore = Depends(get_s3),
) -> Response:
    video = get_owned_video(video_id, db, user)
    asset = _active_asset(db, video.id, "PROXY")

    range_header = request.headers.get("range")
    if range_header:
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
        if not m:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{asset.byte_size}"})
        start_s, end_s = m.groups()
        if not start_s:  # suffix range: bytes=-N
            if not end_s:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{asset.byte_size}"})
            n = min(int(end_s), asset.byte_size)
            start, end = asset.byte_size - n, asset.byte_size - 1
        else:
            start = int(start_s)
            end = min(int(end_s), asset.byte_size - 1) if end_s else asset.byte_size - 1
        if start >= asset.byte_size or start > end:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{asset.byte_size}"})
        body, content_range = s3.get_range(asset.object_key, start, end)
        return Response(
            content=body,
            status_code=206,
            media_type="video/mp4",
            headers={
                "Content-Range": content_range or f"bytes {start}-{end}/{asset.byte_size}",
                "Accept-Ranges": "bytes",
            },
        )

    return Response(
        content=s3.get_bytes(asset.object_key),
        media_type="video/mp4",
        headers={"Accept-Ranges": "bytes"},
    )


@router.get("/{video_id}/thumbs/{ts_ms}.jpg")
def get_thumb(
    video_id: str,
    ts_ms: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    s3: S3ObjectStore = Depends(get_s3),
) -> Response:
    video = get_owned_video(video_id, db, user)
    stream_hint = f"thumbs/{ts_ms}.jpg"
    asset = db.scalar(
        select(MediaAsset).where(
            MediaAsset.video_id == video.id,
            MediaAsset.class_ == "THUMBNAIL",
            MediaAsset.status == "ACTIVE",
            MediaAsset.object_key.endswith(stream_hint),
        )
    )
    if asset is None:
        raise not_found("thumbnail not found")
    return Response(content=s3.get_bytes(asset.object_key), media_type="image/jpeg")
