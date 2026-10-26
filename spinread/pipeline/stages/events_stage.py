"""Publish contact candidates together with inspectable visual replay evidence."""
from __future__ import annotations

from pingpong_training.analysis.replay import replay_evidence
from pingpong_training.analysis.visual_events import analyze_visual_video
from pingpong_training.media.probe import FFmpegError
from spinread.pipeline.stage import StageContext, StageError, StageResult, RetryableStageError
from spinread.pipeline.vision import vision_config, replay_fingerprint


class EventsStage:
    stage = "EVENTS"
    stage_version = "event-evidence-1.0.0"
    cache_fingerprint = staticmethod(replay_fingerprint)

    def run(self, ctx: StageContext) -> StageResult:
        rally_art = ctx.prior_artifacts.get("RALLY")
        if rally_art is None:
            raise StageError("MISSING_INPUT", "EVENTS requires the RALLY artifact")
        payload = ctx.load_artifact_json(rally_art)
        rallies = payload.get("rallies") or []
        config = vision_config(ctx.settings)
        track, contacts = None, []
        duration = ctx.video.duration_ms
        if config is not None:
            local, work = ctx.ensure_original_local()
            try:
                analysis, track, probe = analyze_visual_video(
                    local, work, config, ffmpeg_bin=ctx.settings.ffmpeg_bin,
                    ffprobe_bin=ctx.settings.ffprobe_bin)
            except FFmpegError as exc:
                raise RetryableStageError(f"replay decode failed: {exc}", "FFMPEG_ERROR") from exc
            contacts = analysis.contacts
            duration = probe.duration_ms
        if not duration:
            raise StageError("MISSING_DURATION", "EVENTS requires a source duration")
        replay = replay_evidence(track, contacts, duration)
        if getattr(ctx.settings, "pose_weights", ""):
            from pingpong_training.analysis.pose import PoseConfig, infer_pose
            local, work = ctx.ensure_original_local()
            replay["pose"] = infer_pose(local, work,
                PoseConfig(ctx.settings.pose_weights, fps=ctx.settings.pose_fps),
                ffmpeg_bin=ctx.settings.ffmpeg_bin, ffprobe_bin=ctx.settings.ffprobe_bin)
            replay["capabilities"]["pose_3d"] = "MODEL_ESTIMATE"
        by_time = {c["time_ms"]: c for c in replay["contacts"]}
        events = []
        for idx, rally in enumerate(rallies):
            for t in sorted(set(rally.get("hits_ms") or [])):
                if not rally["start_ms"] <= t < rally["end_ms"]:
                    continue
                contact = by_time.get(t)
                events.append({"type": "HIT_CANDIDATE", "rally_index": idx, "t_ms": int(t),
                               "actor": None, "confidence": None,
                               "evidence": contact or {"kind": "AUDIO_CANDIDATE", "audio_supported": True},
                               "visual_supported": contact is not None})
        return StageResult(
            artifact_name="events.json",
            artifact_json={"stage": self.stage, "stage_version": self.stage_version,
                           "video_id": ctx.video.id, "events": events,
                           "n_rallies": len(rallies), "replay": replay},
            metrics={"n_hit_candidates": len(events), "n_visual_contacts": len(by_time),
                     "n_observed_frames": sum(f[1] is not None for f in replay["frames"])},
            limitations=replay["limitations"],
        )
