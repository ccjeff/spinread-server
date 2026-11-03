"""Visual labeling configuration and cache identity shared by worker stages."""
import hashlib
import json
from dataclasses import asdict
from pingpong_training.analysis.blurball import BlurBallConfig, MODEL_VERSION, _sha256
from pingpong_training.analysis.visual_events import EVENT_VERSION, VisualEventConfig


def vision_config(settings):
    if not getattr(settings, "blurball_weights", ""):
        return None
    return BlurBallConfig(settings.blurball_weights, device=getattr(settings, "blurball_device", "auto"),
                          batch_size=getattr(settings, "blurball_batch_size", 8))


def vision_fingerprint(settings):
    config = vision_config(settings)
    if config is None:
        return "legacy-audio"
    value = {"model":MODEL_VERSION,"weights":_sha256(config.weights),
             "threshold":config.threshold,"events":EVENT_VERSION,"rules":asdict(VisualEventConfig())}
    return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()


def replay_fingerprint(settings):
    from pingpong_training.analysis.pose import POSE_VERSION
    from pingpong_training.analysis.replay import REPLAY_VERSION
    value = {"vision": vision_fingerprint(settings), "replay": REPLAY_VERSION,
             "pose": POSE_VERSION, "pose_fps": getattr(settings, "pose_fps", 12),
             "pose_weights": _sha256(settings.pose_weights) if getattr(settings, "pose_weights", "") else None}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
