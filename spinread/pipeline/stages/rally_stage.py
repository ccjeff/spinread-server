"""RALLY stage (rally-heuristic-0.1.0): cut RALLY_LIKE segments into rallies."""

from __future__ import annotations

import logging

from pingpong_training.analysis import detect_rallies_for_video
from pingpong_training.media.probe import FFmpegError

from spinread.pipeline.stage import (
    RetryableStageError,
    StageContext,
    StageError,
    StageResult,
)
from spinread.pipeline.stages.timeline_stage import promote_item_type

log = logging.getLogger(__name__)


class RallyStage:
    stage = "RALLY"
    stage_version = "rally-heuristic-0.1.0"

    def run(self, ctx: StageContext) -> StageResult:
        activity_art = ctx.prior_artifacts.get("ACTIVITY")
        if activity_art is None:
            raise StageError("MISSING_INPUT", "RALLY requires the ACTIVITY artifact")
        activity = ctx.load_artifact_json(activity_art)

        limitations: list[str] = []
        segments: list[tuple[int, int]] = []
        for item in activity.get("items") or []:
            if promote_item_type(item) == "RALLY_LIKE":
                segments.append((int(item["start_ms"]), int(item["end_ms"])))

        rallies_payload: list[dict] = []
        impact_times: list[int] = []
        if not segments:
            limitations.append("no RALLY_LIKE activity segments: rally detection skipped")
        else:
            local, poc_work = ctx.ensure_original_local()
            try:
                rallies, impact_times = detect_rallies_for_video(
                    local,
                    segments,
                    work_dir=poc_work,
                    ffmpeg_bin=ctx.settings.ffmpeg_bin,
                    ffprobe_bin=ctx.settings.ffprobe_bin,
                )
            except FFmpegError as exc:
                raise RetryableStageError(f"rally media decode failed: {exc}", "FFMPEG_ERROR") from exc

            if not impact_times:
                limitations.append(
                    "no audio track or no impacts: rally candidates empty"
                )
            seg_of = _segment_indexer(segments)
            for r in rallies:
                rallies_payload.append(
                    {
                        "start_ms": r.start_ms,
                        "end_ms": r.end_ms,
                        "confidence": r.confidence,
                        "hits_ms": list(r.hits_ms),
                        "segment_index": seg_of(r.start_ms),
                    }
                )

        return StageResult(
            artifact_name="rallies.json",
            artifact_json={
                "stage": self.stage,
                "stage_version": self.stage_version,
                "video_id": ctx.video.id,
                "rallies": rallies_payload,
                "impact_count": len(impact_times),
                "n_segments": len(segments),
            },
            metrics={
                "n_rallies": len(rallies_payload),
                "impact_count": len(impact_times),
            },
            limitations=limitations or None,
        )


def _segment_indexer(segments: list[tuple[int, int]]):
    def index_of(t_ms: int) -> int | None:
        for i, (s, e) in enumerate(segments):
            if s <= t_ms < e:
                return i
        return None

    return index_of
