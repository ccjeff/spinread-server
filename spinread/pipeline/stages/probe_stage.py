"""PROBE stage (probe-ffprobe-0.1.0): download original, ffprobe, store JSON."""

from __future__ import annotations

import logging

from pingpong_training.media.probe import ProbeError, probe_video
from sqlalchemy import select

from spinread.core.models import MediaAsset
from spinread.pipeline.stage import StageContext, StageError, StageResult

log = logging.getLogger(__name__)

MAX_DURATION_MS = 120 * 60 * 1000  # LLD §4.3: reject >120 min


class ProbeStage:
    stage = "PROBE"
    stage_version = "probe-ffprobe-0.1.0"

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
            probe = probe_video(local, ffprobe_bin=ctx.settings.ffprobe_bin)
        except ProbeError as exc:
            msg = str(exc)
            code = "NO_VIDEO_STREAM" if "no video stream" in msg else "PROBE_FAILED"
            if "non-positive duration" in msg:
                code = "NO_VIDEO_STREAM"
            raise StageError(code, msg) from exc

        if probe.duration_ms > MAX_DURATION_MS:
            raise StageError("DURATION_LIMIT", "video exceeds the 120 minute limit")

        probe_json = probe.model_dump()
        ctx.video.probe = probe_json
        ctx.video.duration_ms = probe.duration_ms
        ctx.session.flush()

        return StageResult(
            artifact_name="probe.json",
            artifact_json={
                "stage": self.stage,
                "stage_version": self.stage_version,
                "video_id": ctx.video.id,
                "probe": probe_json,
            },
            metrics={"duration_ms": probe.duration_ms, "audio_present": probe.audio_present},
        )
