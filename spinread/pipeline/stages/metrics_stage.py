"""METRICS stage (metrics-0.1.0): deterministic metrics over the active timeline.

Reads the active timeline's items (post-edit versions included), upserts
metric_values rows keyed by (video, timeline_version, name, version) and
publishes a metrics.json artifact. Values embed evidence intervals so the
REPORT stage can build findings without re-walking the timeline.
"""

from __future__ import annotations

import logging
import statistics

from sqlalchemy import select

from spinread.core.models import (
    MetricValue,
    Timeline,
    TimelineActivePointer,
    TimelineItem,
)
from spinread.pipeline.stage import StageContext, StageError, StageResult

log = logging.getLogger(__name__)

METRIC_VERSION = "metrics-0.1.0"

_RALLY_LEN_BUCKETS = [(0, 5000), (5000, 15000), (15000, 30000), (30000, None)]


def _items(session, timeline_id: str) -> list[TimelineItem]:
    return session.scalars(
        select(TimelineItem)
        .where(TimelineItem.timeline_id == timeline_id, TimelineItem.status == "ACTIVE")
        .order_by(TimelineItem.start_ms)
    ).all()


class MetricsStage:
    stage = "METRICS"
    stage_version = METRIC_VERSION

    def run(self, ctx: StageContext) -> StageResult:
        pointer = ctx.session.get(TimelineActivePointer, ctx.video.id)
        if pointer is None:
            raise StageError("MISSING_INPUT", "METRICS requires an active timeline")
        timeline = ctx.session.get(Timeline, pointer.timeline_id)
        items = _items(ctx.session, timeline.id)

        top = [i for i in items if i.parent_id is None]
        rallies = [i for i in items if i.type == "RALLY"]
        hits = [i for i in items if i.type == "HIT_CANDIDATE"]
        rally_like = [i for i in top if i.type == "RALLY_LIKE"]

        valid_duration_ms = sum(i.end_ms - i.start_ms for i in rally_like)

        durations = [i.end_ms - i.start_ms for i in rallies]
        rally_duration = {
            "mean": round(statistics.fmean(durations), 1) if durations else 0.0,
            "p50": round(statistics.median(durations), 1) if durations else 0.0,
            "max": max(durations) if durations else 0,
        }
        dist = {"0-5s": 0, "5-15s": 0, "15-30s": 0, "30s+": 0}
        for d in durations:
            for (lo, hi), label in zip(_RALLY_LEN_BUCKETS, dist):
                if d >= lo and (hi is None or d < hi):
                    dist[label] += 1
                    break

        hits_per_rally_map: dict[str, int] = {}
        for h in hits:
            hits_per_rally_map[h.parent_id] = hits_per_rally_map.get(h.parent_id, 0) + 1
        hpr = list(hits_per_rally_map.values())
        hits_per_rally = {
            "mean": round(statistics.fmean(hpr), 2) if hpr else 0.0,
            "max": max(hpr) if hpr else 0,
        }

        seg_type_dist: dict[str, int] = {}
        for i in top:
            seg_type_dist[i.type] = seg_type_dist.get(i.type, 0) + 1

        conf_items = [i for i in items if i.confidence is not None]
        high_conf = [i for i in conf_items if i.confidence >= 0.6]
        confidence_coverage = (
            round(len(high_conf) / len(conf_items), 4) if conf_items else 0.0
        )
        low_conf_intervals = [
            [i.start_ms, i.end_ms] for i in conf_items if i.confidence < 0.6
        ]

        correction_rate = 0.0
        if timeline.version > 1:
            v1 = ctx.session.scalar(
                select(Timeline).where(
                    Timeline.video_id == ctx.video.id, Timeline.version == 1
                )
            )
            if v1 is not None:
                v1_keys = {
                    (i.type, i.start_ms, i.end_ms) for i in _items(ctx.session, v1.id)
                }
                cur_keys = {(i.type, i.start_ms, i.end_ms) for i in items}
                if v1_keys:
                    correction_rate = round(
                        len(v1_keys.symmetric_difference(cur_keys))
                        / (2 * len(v1_keys)),
                        4,
                    )

        non_rally = [
            [i.start_ms, i.end_ms] for i in top if i.type != "RALLY_LIKE"
        ]
        long_rallies = [
            [i.start_ms, i.end_ms] for i in rallies if i.end_ms - i.start_ms > 60_000
        ]

        metrics: dict[str, dict] = {
            "valid_duration_ms": {"value": valid_duration_ms},
            "rally_count": {"value": len(rallies), "evidence": [[i.start_ms, i.end_ms] for i in rallies]},
            "rally_duration_ms": {"value": rally_duration, "evidence": long_rallies},
            "rally_length_distribution": {"value": dist},
            "hits_per_rally": {"value": hits_per_rally},
            "segment_type_distribution": {"value": seg_type_dist, "evidence": non_rally},
            "confidence_coverage": {"value": confidence_coverage, "evidence": low_conf_intervals},
            "correction_rate": {"value": correction_rate},
        }

        video_duration_ms = ctx.video.duration_ms or 0
        for name, payload in metrics.items():
            value_doc = {
                "result": payload["value"],
                "evidence_intervals": payload.get("evidence", []),
                "video_duration_ms": video_duration_ms,
            }
            row = ctx.session.scalar(
                select(MetricValue).where(
                    MetricValue.video_id == ctx.video.id,
                    MetricValue.timeline_version == timeline.version,
                    MetricValue.metric_name == name,
                    MetricValue.metric_version == METRIC_VERSION,
                )
            )
            sample_count = _sample_count(name, items, rallies, top)
            if row is None:
                ctx.session.add(
                    MetricValue(
                        video_id=ctx.video.id,
                        pipeline_run_id=ctx.pipeline_run.id,
                        timeline_version=timeline.version,
                        metric_name=name,
                        metric_version=METRIC_VERSION,
                        value=value_doc,
                        sample_count=sample_count,
                    )
                )
            else:
                row.pipeline_run_id = ctx.pipeline_run.id
                row.value = value_doc
                row.sample_count = sample_count

        ctx.session.flush()
        return StageResult(
            artifact_name="metrics.json",
            artifact_json={
                "stage": self.stage,
                "stage_version": self.stage_version,
                "video_id": ctx.video.id,
                "timeline_version": timeline.version,
                "metrics": {k: v["value"] for k, v in metrics.items()},
            },
            metrics={"timeline_version": timeline.version, "n_items": len(items)},
        )


def _sample_count(name: str, items, rallies, top) -> int:
    if name in ("rally_count", "rally_duration_ms", "rally_length_distribution", "hits_per_rally"):
        return len(rallies)
    if name in ("segment_type_distribution", "valid_duration_ms"):
        return len(top)
    return len(items)
