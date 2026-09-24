"""TIMELINE stage (timeline-build-0.2.0): artifacts -> published timeline.

Hierarchy: ACTIVITY segments are top-level items; RALLY candidates become
children of their segment; HIT_CANDIDATEs become children of their rally
([t, t+40ms) intervals, actor null). When RALLY/EVENTS artifacts are missing
the stage degrades to the flat ACTIVITY-only hierarchy (v0.1.0 behaviour).
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from pingpong_training.analysis.rally import STAGE_VERSION
from pingpong_training.analysis.audio_onset import RACKET_DETECTOR_VERSION

from spinread.core.models import (
    Timeline,
    TimelineActivePointer,
    TimelineItem,
    utcnow,
)
from spinread.pipeline.stage import StageContext, StageError, StageResult

log = logging.getLogger(__name__)

HIT_INTERVAL_MS = 40


def promote_item_type(item: dict) -> str:
    """The POC emits type=ACTIVITY_SEGMENT with the coarse class in
    attributes.activity_type; promote it to the DB item type."""
    item_type = str(item.get("type", "UNKNOWN"))
    attrs = item.get("attributes") or {}
    if item_type == "ACTIVITY_SEGMENT" and attrs.get("activity_type"):
        return str(attrs["activity_type"])
    return item_type


class TimelineStage:
    stage = "TIMELINE"
    stage_version = "timeline-build-0.4.0"

    def run(self, ctx: StageContext) -> StageResult:
        activity_art = ctx.prior_artifacts.get("ACTIVITY")
        if activity_art is None:
            raise StageError("MISSING_INPUT", "TIMELINE requires the ACTIVITY artifact")
        activity = ctx.load_artifact_json(activity_art)

        rally_art = ctx.prior_artifacts.get("RALLY")
        rally_payload = ctx.load_artifact_json(rally_art) if rally_art is not None else {}
        rallies = rally_payload.get("rallies") or []
        rally_version = rally_payload.get("algorithm_version", STAGE_VERSION)
        hit_detector = rally_payload.get("hit_detector", RACKET_DETECTOR_VERSION)
        events_art = ctx.prior_artifacts.get("EVENTS")
        events = (
            (ctx.load_artifact_json(events_art).get("events") or [])
            if events_art is not None
            else []
        )

        max_version = ctx.session.scalar(
            select(func.max(Timeline.version)).where(Timeline.video_id == ctx.video.id)
        ) or 0
        timeline = Timeline(
            video_id=ctx.video.id,
            version=max_version + 1,
            state="PUBLISHED",
            created_by="MODEL",
        )
        ctx.session.add(timeline)
        ctx.session.flush()

        limitations = list(activity.get("limitations") or [])
        n_items = 0
        seg_rows: list[TimelineItem] = []  # top-level segments in start order

        for item in activity.get("items") or []:
            attrs = dict(item.get("attributes") or {})
            attrs["poc_item_id"] = item.get("item_id")
            row = TimelineItem(
                timeline_id=timeline.id,
                type=promote_item_type(item),
                start_ms=int(item["start_ms"]),
                end_ms=int(item["end_ms"]),
                actor=item.get("actor"),
                attributes=attrs,
                confidence=item.get("confidence"),
                provenance=item.get("provenance") or {},
                status=item.get("status", "ACTIVE"),
            )
            if row.end_ms <= row.start_ms:
                continue  # keep the CHECK constraint intact on degenerate items
            ctx.session.add(row)
            ctx.session.flush()
            seg_rows.append(row)
            n_items += 1

        seg_rows.sort(key=lambda r: r.start_ms)
        rally_rows: dict[int, TimelineItem] = {}  # rallies.json index -> row
        for idx, rally in enumerate(rallies):
            parent = _parent_segment(seg_rows, int(rally["start_ms"]))
            if parent is None:
                continue
            if int(rally["end_ms"]) <= int(rally["start_ms"]):
                continue
            row = TimelineItem(
                timeline_id=timeline.id,
                parent_id=parent.id,
                type="RALLY",
                start_ms=int(rally["start_ms"]),
                end_ms=int(rally["end_ms"]),
                attributes={
                    "hits": len(rally.get("hits_ms") or []),
                    "poc": rally_version,
                    "hit_detector": hit_detector,
                    "hit_count_estimated": True,
                },
                confidence=rally.get("confidence"),
                provenance={"source": "MODEL", "source_id": rally_version},
                status="ACTIVE",
            )
            ctx.session.add(row)
            ctx.session.flush()
            rally_rows[idx] = row
            n_items += 1

        n_hits = 0
        for event in events:
            rally_idx = event.get("rally_index")
            parent = rally_rows.get(rally_idx) if rally_idx is not None else None
            if parent is None:
                continue
            t_ms = int(event["t_ms"])
            start = max(t_ms, parent.start_ms)
            end = min(t_ms + HIT_INTERVAL_MS, parent.end_ms)
            if end <= start:
                end = start + 1  # CHECK end_ms > start_ms
            row = TimelineItem(
                timeline_id=timeline.id,
                parent_id=parent.id,
                type="HIT_CANDIDATE",
                start_ms=start,
                end_ms=end,
                actor=None,
                attributes={"poc": hit_detector},
                confidence=None,
                provenance={"source": "MODEL", "source_id": hit_detector},
                status="ACTIVE",
            )
            ctx.session.add(row)
            n_items += 1
            n_hits += 1

        # Atomic pointer switch (UPSERT on video_id PK).
        pointer = ctx.session.get(TimelineActivePointer, ctx.video.id)
        if pointer is None:
            pointer = TimelineActivePointer(
                video_id=ctx.video.id, timeline_id=timeline.id, switched_at=utcnow()
            )
            ctx.session.add(pointer)
        else:
            pointer.timeline_id = timeline.id
            pointer.switched_at = utcnow()

        ctx.session.flush()
        return StageResult(
            metrics={
                "timeline_version": timeline.version,
                "n_items": n_items,
                "n_rallies": len(rally_rows),
                "n_hit_candidates": n_hits,
            },
            limitations=limitations or None,
        )
def _parent_segment(segments: list[TimelineItem], t_ms: int) -> TimelineItem | None:
    for seg in segments:
        if seg.start_ms <= t_ms < seg.end_ms:
            return seg
    return None
