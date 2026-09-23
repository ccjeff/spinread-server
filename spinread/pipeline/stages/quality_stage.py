"""QUALITY stage (quality-heuristic-0.1.0, simplified).

Emits the HLD §7.2 capability set derived from the probe artifact only:
activity segmentation is always SUPPORTED at P0; audio-dependent capabilities
degrade when no audio track is present.
"""

from __future__ import annotations

import logging

from spinread.pipeline.stage import StageContext, StageError, StageResult

log = logging.getLogger(__name__)


class QualityStage:
    stage = "QUALITY"
    stage_version = "quality-heuristic-0.1.0"

    def run(self, ctx: StageContext) -> StageResult:
        probe_art = ctx.prior_artifacts.get("PROBE")
        if probe_art is None:
            raise StageError("MISSING_INPUT", "QUALITY requires the PROBE artifact")
        probe_payload = ctx.load_artifact_json(probe_art)
        probe = probe_payload.get("probe") or {}
        audio_present = bool(probe.get("audio_present"))

        issues: list[dict] = []
        capabilities = {
            "activity_segmentation": "SUPPORTED",
            "rally_segmentation": "UNSUPPORTED",
            "hit_candidates": "SUPPORTED" if audio_present else "UNSUPPORTED",
            "ball_trajectory": "UNSUPPORTED",
            "scoreboard_ocr": "NOT_APPLICABLE",
            "spin_classification": "UNSUPPORTED",
        }
        if not audio_present:
            issues.append(
                {
                    "code": "NO_AUDIO_TRACK",
                    "severity": "MEDIUM",
                    "intervals": [],
                    "user_message": "No audio track: hit detection is unavailable.",
                }
            )

        artifact = {
            "stage": self.stage,
            "stage_version": self.stage_version,
            "video_id": ctx.video.id,
            "overall": "PARTIAL",
            "capabilities": capabilities,
            "issues": issues,
            "config": "quality-config-0.1",
        }
        return StageResult(
            artifact_name="quality.json",
            artifact_json=artifact,
            metrics={"audio_present": audio_present, "capabilities": capabilities},
            limitations=[
                "MLP heuristic: capabilities derived from probe metadata only"
            ],
        )
