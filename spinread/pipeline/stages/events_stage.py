"""EVENTS stage (event-flat-0.1.0): flatten rally hits into HIT_CANDIDATEs."""

from __future__ import annotations

import logging

from spinread.pipeline.stage import StageContext, StageError, StageResult

log = logging.getLogger(__name__)


class EventsStage:
    stage = "EVENTS"
    stage_version = "event-flat-0.1.0"

    def run(self, ctx: StageContext) -> StageResult:
        rally_art = ctx.prior_artifacts.get("RALLY")
        if rally_art is None:
            raise StageError("MISSING_INPUT", "EVENTS requires the RALLY artifact")
        payload = ctx.load_artifact_json(rally_art)
        rallies = payload.get("rallies") or []

        events: list[dict] = []
        for idx, rally in enumerate(rallies):
            for t_ms in rally.get("hits_ms") or []:
                events.append(
                    {
                        "type": "HIT_CANDIDATE",
                        "rally_index": idx,
                        "t_ms": int(t_ms),
                        "actor": None,
                        "confidence": rally.get("confidence"),
                    }
                )

        return StageResult(
            artifact_name="events.json",
            artifact_json={
                "stage": self.stage,
                "stage_version": self.stage_version,
                "video_id": ctx.video.id,
                "events": events,
                "n_rallies": len(rallies),
            },
            metrics={"n_hit_candidates": len(events)},
            limitations=[
                "no player localization at P0: HIT_CANDIDATE actor is null"
            ],
        )
