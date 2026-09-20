"""TIMELINE stage (timeline-build-0.1.0): ACTIVITY artifact -> published timeline.

Creates a new timelines row (version = previous + 1, PUBLISHED, MODEL),
copies POC items into timeline_items (poc seg_ ids preserved in
attributes.poc_item_id), and UPSERTs the active pointer. No artifact row is
produced — the timeline tables are the output.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from spinread.core.models import (
    Timeline,
    TimelineActivePointer,
    TimelineItem,
    utcnow,
)
from spinread.pipeline.stage import StageContext, StageError, StageResult

log = logging.getLogger(__name__)


class TimelineStage:
    stage = "TIMELINE"
    stage_version = "timeline-build-0.1.0"

    def run(self, ctx: StageContext) -> StageResult:
        activity_art = ctx.prior_artifacts.get("ACTIVITY")
        if activity_art is None:
            raise StageError("MISSING_INPUT", "TIMELINE requires the ACTIVITY artifact")
        payload = ctx.load_artifact_json(activity_art)
        items = payload.get("items") or []

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

        n_items = 0
        for item in items:
            attrs = dict(item.get("attributes") or {})
            attrs["poc_item_id"] = item.get("item_id")
            # The POC emits type=ACTIVITY_SEGMENT with the coarse class in
            # attributes.activity_type; promote it to the DB item type.
            item_type = str(item.get("type", "UNKNOWN"))
            if item_type == "ACTIVITY_SEGMENT" and attrs.get("activity_type"):
                item_type = str(attrs["activity_type"])
            row = TimelineItem(
                timeline_id=timeline.id,
                type=item_type,
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
            n_items += 1

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
            metrics={"timeline_version": timeline.version, "n_items": n_items},
            limitations=payload.get("limitations") or [],
        )
