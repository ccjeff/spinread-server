"""ACTIVITY stage: run the POC analysis chain (activity-heuristic-0.1.0)."""

from __future__ import annotations

import json
import logging

from pingpong_training.media.probe import FFmpegError
from pingpong_training.pipeline import analyze_video
from spinread.pipeline.vision import vision_config, vision_fingerprint

from spinread.pipeline.stage import (
    RetryableStageError,
    StageContext,
    StageError,
    StageResult,
)

log = logging.getLogger(__name__)


class ActivityStage:
    stage = "ACTIVITY"
    stage_version = "activity-selectable-1.0.0"
    cache_fingerprint = staticmethod(vision_fingerprint)

    def run(self, ctx: StageContext) -> StageResult:
        # Stable cross-run cache: pcm/gray_frames npy caches hit across runs.
        local, poc_work = ctx.ensure_original_local()

        try:
            artifact = analyze_video(
                local,
                work_dir=poc_work,
                blurball_config=vision_config(ctx.settings),
                ffmpeg_bin=ctx.settings.ffmpeg_bin,
                ffprobe_bin=ctx.settings.ffprobe_bin,
            )
        except FFmpegError as exc:
            raise RetryableStageError(f"analysis media decode failed: {exc}", "FFMPEG_ERROR") from exc
        except Exception as exc:  # heuristic must degrade, not crash the worker
            raise RetryableStageError(f"activity analysis failed: {exc}", "ANALYSIS_ERROR") from exc

        payload = json.loads(artifact.model_dump_json())
        return StageResult(
            artifact_name="activity.json",
            artifact_json=payload,
            metrics=artifact.metrics,
            limitations=artifact.limitations,
        )
