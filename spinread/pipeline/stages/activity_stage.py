"""ACTIVITY stage: run the POC analysis chain (activity-heuristic-0.1.0)."""

from __future__ import annotations

import json
import logging

from pingpong_training.media.probe import FFmpegError
from pingpong_training.pipeline import analyze_video
from sqlalchemy import select

from spinread.core.models import MediaAsset
from spinread.pipeline.stage import (
    RetryableStageError,
    StageContext,
    StageError,
    StageResult,
)

log = logging.getLogger(__name__)


class ActivityStage:
    stage = "ACTIVITY"
    stage_version = "activity-heuristic-0.1.0"

    def run(self, ctx: StageContext) -> StageResult:
        original = ctx.session.scalar(
            select(MediaAsset).where(
                MediaAsset.video_id == ctx.video.id,
                MediaAsset.class_ == "ORIGINAL",
                MediaAsset.status == "ACTIVE",
            )
        )
        if original is None:
            raise StageError("NO_ORIGINAL", "video has no ACTIVE ORIGINAL media asset")

        local = ctx.work_dir / "original_input"
        ctx.s3.download_file(original.object_key, str(local))

        try:
            artifact = analyze_video(
                local,
                work_dir=ctx.work_dir / "poc_cache",
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
