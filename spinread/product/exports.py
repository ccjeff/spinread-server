"""Clip / highlight exports (HLD §9.2–9.3): idempotent manifest creation."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.core import queue
from spinread.core.models import (
    ExportManifest,
    Job,
    MediaAsset,
    TimelineItem,
    Video,
)

CLIP_PRESET = "clip-720p-v1"
HIGHLIGHT_PRESET = "highlight-720p-v1"

DEFAULT_PRE_ROLL_MS = 800
DEFAULT_POST_ROLL_MS = 1200


class ExportError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _idempotency_hash(
    video_id: str,
    proxy_hash: str,
    intervals: list[tuple[int, int]],
    preset: str,
    pre_roll_ms: int,
    post_roll_ms: int,
) -> str:
    canonical = json.dumps(
        {
            "video_id": video_id,
            "proxy_content_hash": proxy_hash,
            "intervals": [[int(s), int(e)] for s, e in intervals],
            "preset": preset,
            "pre_roll_ms": int(pre_roll_ms),
            "post_roll_ms": int(post_roll_ms),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _active_proxy(session: Session, video_id: str) -> MediaAsset:
    proxy = session.scalar(
        select(MediaAsset).where(
            MediaAsset.video_id == video_id,
            MediaAsset.class_ == "PROXY",
            MediaAsset.status == "ACTIVE",
        )
    )
    if proxy is None:
        raise ExportError("MEDIA_NOT_READY", "proxy is not rendered yet")
    return proxy


def _active_timeline_version(session: Session, video_id: str) -> int:
    from spinread.core.models import Timeline, TimelineActivePointer

    pointer = session.get(TimelineActivePointer, video_id)
    if pointer is None:
        raise ExportError("NO_TIMELINE", "video has no active timeline")
    return session.get(Timeline, pointer.timeline_id).version


def create_export(
    session: Session,
    video: Video,
    kind: str,
    intervals: list[tuple[int, int]],
    preset: str,
    pre_roll_ms: int,
    post_roll_ms: int,
) -> tuple[ExportManifest, bool]:
    """Create (or reuse) an export manifest and enqueue rendering.

    Returns (manifest, created) — created=False on idempotent replay.
    """
    if not intervals:
        raise ExportError("NO_INTERVALS", "at least one interval is required")
    duration = video.duration_ms
    clamped: list[tuple[int, int]] = []
    for start, end in intervals:
        s = max(0, int(start))
        e = min(int(end), duration) if duration else int(end)
        if e > s:
            clamped.append((s, e))
    if not clamped:
        raise ExportError("NO_INTERVALS", "intervals are empty after clamping")

    proxy = _active_proxy(session, video.id)
    idem = _idempotency_hash(
        video.id, proxy.content_hash, clamped, preset, pre_roll_ms, post_roll_ms
    )
    existing = session.scalar(
        select(ExportManifest).where(ExportManifest.idempotency_hash == idem)
    )
    if existing is not None:
        return existing, False

    manifest = ExportManifest(
        video_id=video.id,
        kind=kind,
        timeline_version=_active_timeline_version(session, video.id),
        intervals=[[s, e] for s, e in clamped],
        render_preset=preset,
        idempotency_hash=idem,
    )
    session.add(manifest)
    session.flush()
    queue.enqueue(
        session,
        "CLIP_RENDER",
        {
            "export_id": manifest.id,
            "pre_roll_ms": pre_roll_ms,
            "post_roll_ms": post_roll_ms,
        },
        max_attempts=2,
    )
    return manifest, True


def export_status(session: Session, manifest: ExportManifest) -> str:
    """RENDERING / READY / FAILED, derived from asset + open job state."""
    if manifest.asset_id is not None:
        return "READY"
    open_job = session.scalar(
        select(Job.id).where(
            Job.kind == "CLIP_RENDER",
            Job.done_at.is_(None),
            Job.payload["export_id"].as_string() == manifest.id,
        )
    )
    return "RENDERING" if open_job is not None else "FAILED"
