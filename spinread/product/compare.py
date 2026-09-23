"""Retest comparability is a gate, not a retrospective ability score."""
import math
from sqlalchemy import select
from spinread.core.models import Artifact, MetricValue, Timeline, TimelineActivePointer, Video

METRICS = {
    "active_fraction": ("valid_duration_ms", "activity_segmentation"),
    "confidence_coverage": ("confidence_coverage", "activity_segmentation"),
    "rally_duration_ms.mean": ("rally_duration_ms", "rally_segmentation"),
    "rally_duration_ms.max": ("rally_duration_ms", "rally_segmentation"),
    "hits_per_rally.mean": ("hits_per_rally", "hit_candidates"),
}


def report_current(db, report):
    p = db.get(TimelineActivePointer, report.video_id)
    return bool(p and db.get(Timeline, p.timeline_id).version == report.timeline_version and report.state == "PUBLISHED")


def snapshot(db, report, metric):
    video = db.get(Video, report.video_id)
    base, capability = METRICS[metric]
    row = db.scalar(select(MetricValue).where(MetricValue.video_id == video.id,
        MetricValue.timeline_version == report.timeline_version, MetricValue.metric_name == base,
        MetricValue.metric_version == report.metric_versions.get(base)))
    quality = db.scalar(select(Artifact).where(Artifact.video_id == video.id, Artifact.stage == "QUALITY",
        Artifact.created_at <= report.published_at).order_by(Artifact.created_at.desc()).limit(1)) if report.published_at else None
    caps = (quality.metrics or {}).get("capabilities", {}) if quality else {}
    # Legacy quality artifacts explicitly supported activity and, with audio,
    # hit candidates. Do not upgrade their unsupported rally capability.
    if quality and not caps and quality.stage_version == "quality-heuristic-0.1.0":
        caps = {"activity_segmentation": "SUPPORTED", "rally_segmentation": "UNSUPPORTED",
                "hit_candidates": "SUPPORTED" if quality.metrics.get("audio_present") else "UNSUPPORTED"}
    raw = report.structured.get("metrics", {}).get(base)
    if metric == "active_fraction":
        value = raw / video.duration_ms if isinstance(raw, (int, float)) and video.duration_ms else None
    elif "." in metric:
        value = raw.get(metric.split(".")[1]) if isinstance(raw, dict) else None
    else:
        value = raw
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        value = None
    return {"report_id": report.id, "video_id": video.id, "timeline_version": report.timeline_version,
        "metric_version": report.metric_versions.get(base), "value": value,
        "sample_count": row.sample_count if row else 0, "session_type": video.session_type,
        "context": video.training_context, "context_version": video.context_version,
        "capability": caps.get(capability, "UNKNOWN"), "quality_version": quality.stage_version if quality else None,
        "current": report_current(db, report), "deleted": video.deleted_at is not None}


def compare(db, baseline_report, retest_report, criteria):
    metric = criteria["metric"]
    a, b = snapshot(db, baseline_report, metric), snapshot(db, retest_report, metric)
    reasons = []
    if a["video_id"] == b["video_id"]: reasons.append("SAME_VIDEO")
    if a["deleted"] or b["deleted"]: reasons.append("VIDEO_DELETED")
    if not a["current"]: reasons.append("BASELINE_REPORT_STALE")
    if not b["current"]: reasons.append("RETEST_REPORT_STALE")
    if a["session_type"] != b["session_type"]: reasons.append("SESSION_TYPE_MISMATCH")
    if not a["metric_version"] or a["metric_version"] != b["metric_version"]: reasons.append("METRIC_VERSION_MISMATCH")
    for name in ("practice_context", "target_identity", "camera_setup", "opponent_or_feeder"):
        left, right = a["context"].get(name), b["context"].get(name)
        if not left or not right: reasons.append(f"MISSING_{name.upper()}")
        elif left != right: reasons.append(f"{name.upper()}_MISMATCH")
    if a["context"].get("practice_context") != criteria["context"]:
        reasons.append("PLAN_CONTEXT_MISMATCH")
    if a["capability"] not in ("SUPPORTED", "SUPPORTED_WITH_LOW_CONFIDENCE") or b["capability"] not in ("SUPPORTED", "SUPPORTED_WITH_LOW_CONFIDENCE"):
        reasons.append("CAPABILITY_UNSUPPORTED")
    if not a["quality_version"] or a["quality_version"] != b["quality_version"]:
        reasons.append("QUALITY_VERSION_MISMATCH")
    if min(a["sample_count"], b["sample_count"]) < criteria["minimum_samples"]:
        reasons.append("INSUFFICIENT_SAMPLES")
    if a["value"] is None or b["value"] is None: reasons.append("METRIC_UNAVAILABLE")
    elif a["value"] == 0: reasons.append("ZERO_BASELINE")
    result = {"comparable": not reasons, "reasons": reasons, "metric": metric,
        "baseline": a, "retest": b, "criteria": criteria, "delta": None,
        "relative_change": None, "success": None,
        "limitations": ["对比依赖当前启发式检测和用户提供的录制场景；仅供复盘，不代表技术能力评分。"]}
    if not reasons:
        delta = b["value"] - a["value"]
        relative = delta / abs(a["value"])
        signed = relative if criteria["direction"] == "increase" else -relative
        result.update(delta=delta, relative_change=relative, success=signed >= criteria["relative_threshold"])
    return result
