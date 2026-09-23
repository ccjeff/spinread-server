"""REPORT stage (report-0.1.0): metric_values -> analysis_reports + findings.

Deterministic rule findings (no LLM at P0):
1. COVERAGE      — low-confidence item ratio > 0.3
2. ACTIVITY_MIX  — non-RALLY_LIKE time ratio > 0.25
3. RALLY_LENGTH  — at least one rally longer than 60 s (may be under-segmented)

These legacy rules produce INTERNAL engineering diagnostics only.
Player delivery and plan generation exclude these categories.
Previous PUBLISHED reports of the video flip to SUPERSEDED.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from spinread.core.models import (
    AnalysisReport,
    Finding,
    MetricValue,
    Timeline,
    TimelineActivePointer,
    utcnow,
)
from spinread.pipeline.stage import StageContext, StageError, StageResult

log = logging.getLogger(__name__)

REPORT_VERSION = "report-0.2.0"

# HLD §10.2: evidence is a *representative* sample, never the full set.
MAX_EVIDENCE_INTERVALS = 20


def _cap_evidence(evidence: list) -> tuple[list, str | None]:
    """Keep at most MAX_EVIDENCE_INTERVALS entries (order preserved — the
    metrics stage already orders by relevance). Returns (shown, limitation)."""
    total = len(evidence)
    if total <= MAX_EVIDENCE_INTERVALS:
        return evidence, None
    shown = evidence[:MAX_EVIDENCE_INTERVALS]
    return shown, f"evidence truncated: {total} intervals, showing first {len(shown)}"


class ReportStage:
    stage = "REPORT"
    stage_version = REPORT_VERSION

    def run(self, ctx: StageContext) -> StageResult:
        pointer = ctx.session.get(TimelineActivePointer, ctx.video.id)
        if pointer is None:
            raise StageError("MISSING_INPUT", "REPORT requires an active timeline")
        timeline = ctx.session.get(Timeline, pointer.timeline_id)

        rows = ctx.session.scalars(
            select(MetricValue).where(
                MetricValue.video_id == ctx.video.id,
                MetricValue.timeline_version == timeline.version,
            )
        ).all()
        if not rows:
            raise StageError("MISSING_INPUT", "REPORT requires metric_values rows")

        metrics = {r.metric_name: r for r in rows}
        metric_versions = {r.metric_name: r.metric_version for r in rows}

        def result(name: str):
            return metrics[name].value["result"]

        def evidence(name: str) -> list:
            return metrics[name].value.get("evidence_intervals") or []

        def sample(name: str) -> int:
            return metrics[name].sample_count

        findings: list[Finding] = []

        def add(category: str, observation: str, ev: list, n: int, score: float, limits: list[str] | None = None):
            state = "INTERNAL"  # Detector diagnostics never become player advice.
            shown, truncation_note = _cap_evidence(ev)
            limitations = list(limits or [])
            if truncation_note is not None:
                limitations.append(truncation_note)
            findings.append(
                Finding(
                    report_id="",  # filled after report flush
                    category=category,
                    observation=observation,
                    evidence_intervals=shown,
                    sample_count=n,
                    priority_score=score,
                    limitations=limitations,
                    state=state,
                )
            )

        # rule 1: low-confidence coverage
        coverage = float(result("confidence_coverage"))
        low_ratio = round(1.0 - coverage, 4)
        if low_ratio > 0.3:
            add(
                "COVERAGE",
                f"{low_ratio:.0%} of timeline items have confidence below 0.6; "
                "uncalibrated detector output; internal diagnostic only.",
                evidence("confidence_coverage"),
                sample("confidence_coverage"),
                min(1.0, low_ratio),
                ["confidence is an uncalibrated heuristic"],
            )

        # rule 2: non-rally time share
        video_ms = metrics["valid_duration_ms"].value.get("video_duration_ms") or 0
        valid_ms = int(result("valid_duration_ms"))
        non_rally_ratio = (
            round(1.0 - valid_ms / video_ms, 4) if video_ms > 0 else 0.0
        )
        if non_rally_ratio > 0.25:
            add(
                "ACTIVITY_MIX",
                f"{non_rally_ratio:.0%} of the session is not rally-like "
                "(breaks / ball pickup / instruction); effective practice time is limited.",
                evidence("segment_type_distribution"),
                sample("segment_type_distribution"),
                min(1.0, non_rally_ratio),
            )

        # rule 3: outlier rally length
        long_rallies = evidence("rally_duration_ms")
        if long_rallies:
            add(
                "RALLY_LENGTH",
                f"{len(long_rallies)} rally/rallies exceed 60 s; the segmentation "
                "may be too coarse around dead-ball periods.",
                long_rallies,
                sample("rally_count"),
                0.5,
                ["rally boundaries come from audio impact spacing only"],
            )

        # supersede previous PUBLISHED reports
        previous = ctx.session.scalars(
            select(AnalysisReport).where(
                AnalysisReport.video_id == ctx.video.id,
                AnalysisReport.state == "PUBLISHED",
            )
        ).all()
        for old in previous:
            old.state = "SUPERSEDED"

        report = AnalysisReport(
            video_id=ctx.video.id,
            timeline_version=timeline.version,
            metric_versions=metric_versions,
            state="PUBLISHED",
            structured={
                "timeline_version": timeline.version,
                "metrics": {name: r.value["result"] for name, r in metrics.items()},
            },
            published_at=utcnow(),
        )
        ctx.session.add(report)
        ctx.session.flush()
        for f in findings:
            f.report_id = report.id
            ctx.session.add(f)
        ctx.session.flush()

        return StageResult(
            artifact_name="report.json",
            artifact_json={
                "stage": self.stage,
                "stage_version": self.stage_version,
                "video_id": ctx.video.id,
                "report_id": report.id,
                "timeline_version": timeline.version,
                "n_findings": len(findings),
            },
            metrics={
                "n_findings": len(findings),
                "n_published": sum(1 for f in findings if f.state == "PUBLISHED"),
            },
        )
