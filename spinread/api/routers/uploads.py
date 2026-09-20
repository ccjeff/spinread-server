"""Upload flow (LLD §4.1): presigned multipart direct-to-S3, complete, status.

The API never proxies upload bytes. Presigned URLs are never logged.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from spinread.api.deps import get_current_user, get_db, get_owned_video, get_s3
from spinread.api.errors import bad_request, not_found
from spinread.api.schemas import (
    CompleteUploadRequest,
    CompleteUploadResponse,
    CreateUploadRequest,
    CreateUploadResponse,
    UploadPart,
    UploadSessionOut,
)
from spinread.config import Settings, get_settings
from spinread.core import queue
from spinread.core.models import UploadSession, User, Video, utcnow
from spinread.core.storage import S3ObjectStore, original_key
from spinread.pipeline.finalize import finalize_upload

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/video-uploads", tags=["video-uploads"])


@router.post("", response_model=CreateUploadResponse, status_code=201)
def create_upload(
    body: CreateUploadRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    s3: S3ObjectStore = Depends(get_s3),
    settings: Settings = Depends(get_settings),
) -> CreateUploadResponse:
    if body.byte_size > settings.max_upload_bytes:
        raise bad_request(
            "FILE_TOO_LARGE",
            f"byte_size {body.byte_size} exceeds the 4 GiB limit",
            {"max_bytes": settings.max_upload_bytes},
        )
    if not body.content_type.startswith("video/"):
        raise bad_request("INVALID_CONTENT_TYPE", "content_type must be video/*")

    video = Video(
        owner_id=user.id,
        state="UPLOAD_PENDING",
        filename=body.filename,
        session_type=body.session_type,
        target_player=body.target_player.model_dump(),
        recorded_at=body.recorded_at,
    )
    db.add(video)
    db.flush()

    session = UploadSession(
        video_id=video.id,
        provider_upload_id="pending",
        expected_bytes=body.byte_size,
        state="OPEN",
        object_key=original_key(user.id, video.id, "pending", body.filename),
        expires_at=utcnow() + timedelta(seconds=settings.presign_expiry_seconds),
    )
    db.add(session)
    db.flush()

    # Now that we have the upload id, finalize the object key.
    session.object_key = original_key(user.id, video.id, session.id, body.filename)
    upload_id = s3.create_multipart_upload(session.object_key, body.content_type)
    session.provider_upload_id = upload_id

    n_parts = math.ceil(body.byte_size / settings.upload_part_size)
    parts = [
        UploadPart(
            part_number=n,
            presigned_url=s3.presign_put_part(
                session.object_key, upload_id, n, settings.presign_expiry_seconds
            ),
        )
        for n in range(1, n_parts + 1)
    ]
    db.flush()
    log.info("created upload session %s for video %s (%d parts)", session.id, video.id, n_parts)
    return CreateUploadResponse(
        video_id=video.id,
        upload_id=session.id,
        part_size=settings.upload_part_size,
        parts=parts,
        expires_at=session.expires_at,
    )


@router.post("/{upload_id}/complete", response_model=CompleteUploadResponse)
def complete_upload(
    upload_id: str,
    body: CompleteUploadRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
    s3: S3ObjectStore = Depends(get_s3),
    settings: Settings = Depends(get_settings),
) -> CompleteUploadResponse:
    session = db.get(UploadSession, upload_id)
    if session is None:
        raise not_found("upload session not found")
    video = get_owned_video(session.video_id, db, user)

    if session.state == "OPEN":
        if not body.parts:
            raise bad_request("NO_PARTS", "parts list is empty")
        parts = sorted(
            ({"PartNumber": p.part_number, "ETag": p.etag} for p in body.parts),
            key=lambda p: p["PartNumber"],
        )
        try:
            s3.complete_multipart_upload(session.object_key, session.provider_upload_id, parts)
        except Exception as exc:
            log.warning("CompleteMultipartUpload failed for %s: %s", upload_id, type(exc).__name__)
            raise bad_request(
                "MULTIPART_COMPLETE_FAILED",
                "could not complete the multipart upload; check part numbers/etags",
            )
        # Finalize synchronously: it is fast (HeadObject + hash + rows).
        state = finalize_upload(db, settings, s3, session)
        if state == "UPLOADED":
            # Idempotent backstop: the synchronous finalize above already did
            # the work; the job handler safely no-ops on replay.
            queue.enqueue(db, "FINALIZE_UPLOAD", {"upload_session_id": session.id})
        return CompleteUploadResponse(video_id=video.id, state=state)

    # Idempotent replay: multipart already completed; just report state.
    return CompleteUploadResponse(video_id=video.id, state=video.state)


@router.get("/{upload_id}", response_model=UploadSessionOut)
def get_upload(
    upload_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UploadSessionOut:
    session = db.get(UploadSession, upload_id)
    if session is None:
        raise not_found("upload session not found")
    get_owned_video(session.video_id, db, user)
    return UploadSessionOut(
        upload_id=session.id,
        video_id=session.video_id,
        state=session.state,
        expected_bytes=session.expected_bytes,
        expires_at=session.expires_at,
    )
