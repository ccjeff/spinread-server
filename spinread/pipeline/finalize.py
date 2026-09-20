"""Finalize-upload logic, shared by the /complete endpoint and the job handler.

Idempotent on upload_session id (LLD §4.1 step 4).
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.config import Settings
from spinread.core.models import (
    MediaAsset,
    PipelineRun,
    UploadSession,
    Video,
)
from spinread.pipeline.dag import PIPELINE_VERSION
from spinread.pipeline import orchestrator

log = logging.getLogger(__name__)


def finalize_upload(
    session: Session, settings: Settings, s3, upload_session: UploadSession
) -> str:
    """Verify size, register ORIGINAL asset, open pipeline run. Returns video state."""
    video = session.get(Video, upload_session.video_id)
    if video is None:
        upload_session.state = "ABORTED"
        session.flush()
        return "PERMANENT_FAILURE"
    if upload_session.state == "COMPLETED" or video.state not in (
        "UPLOAD_PENDING",
        "UPLOADING_FINALIZE",
    ):
        return video.state  # idempotent replay

    upload_session.state = "FINALIZING"
    session.flush()

    head = s3.head(upload_session.object_key)
    if head is None or int(head.get("ContentLength", -1)) != upload_session.expected_bytes:
        actual = None if head is None else head.get("ContentLength")
        log.warning(
            "upload %s size mismatch: expected %s got %s",
            upload_session.id, upload_session.expected_bytes, actual,
        )
        upload_session.state = "ABORTED"
        video.state = "PERMANENT_FAILURE"
        session.flush()
        return video.state

    # Content hash: stream the object through sha256 (P0 videos are small enough).
    import hashlib

    h = hashlib.sha256()
    body = s3.client.get_object(Bucket=s3.bucket, Key=upload_session.object_key)["Body"]
    while True:
        chunk = body.read(1024 * 1024)
        if not chunk:
            break
        h.update(chunk)

    existing = session.scalar(
        select(MediaAsset).where(
            MediaAsset.video_id == video.id, MediaAsset.class_ == "ORIGINAL"
        )
    )
    if existing is None:
        session.add(
            MediaAsset(
                video_id=video.id,
                class_="ORIGINAL",
                media_version=0,
                object_key=upload_session.object_key,
                content_hash=h.hexdigest(),
                byte_size=upload_session.expected_bytes,
                status="ACTIVE",
            )
        )

    video.state = "UPLOADED"
    upload_session.state = "COMPLETED"
    final_state = video.state

    run = PipelineRun(
        video_id=video.id, pipeline_version=PIPELINE_VERSION, trigger="UPLOAD"
    )
    session.add(run)
    session.flush()
    orchestrator.tick(session, run.id)
    session.flush()
    return final_state
