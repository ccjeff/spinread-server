"""Async, repeatable candidate preparation using the configured hit detector."""
import logging
import numpy as np
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import select
from pingpong_training.analysis.rally import detect_rallies_for_video
from spinread.pipeline.vision import vision_config
from pingpong_training.analysis.serve import DETECTOR_VERSION, detect_serve_candidates
from spinread.core.models import MediaAsset, QuizGeneration, QuizItem, Video, PracticeWindow
from pingpong_training.analysis.practice import FEATURE_VERSION, window_features
from pingpong_training.analysis.motion import extract_gray_frames
from pingpong_training.pipeline import _load_cache, _save_cache


def practice_features(local, work, impacts, duration_ms, settings):
    meta = {"path": str(local.resolve()), "size": local.stat().st_size, "mtime_ns": local.stat().st_mtime_ns}
    try:
        frames = _load_cache(work, "gray_frames", meta)
        if frames is None:
            frames, _ = extract_gray_frames(local, ffmpeg_bin=settings.ffmpeg_bin, ffprobe_bin=settings.ffprobe_bin)
            _save_cache(work, "gray_frames", frames, meta)
    except Exception:
        log.exception("practice visual features unavailable; abstaining")
        frames = np.zeros(0)
    return window_features(frames, impacts, duration_ms)

log = logging.getLogger(__name__)


def generate_quiz_candidates(session, settings, s3, job):
    g = session.scalar(select(QuizGeneration).where(QuizGeneration.id == job.payload["generation_id"]).with_for_update())
    if g is None or g.status == "READY":
        return "done"
    generation_id = g.id
    try:
        video = session.get(Video, g.video_id)
        if video is None or video.deleted_at is not None:
            g.status, g.error = "FAILED", "视频已不可用"
            session.commit()
            return "done"
        g.status = "RUNNING"
        session.commit()
        original = session.scalar(select(MediaAsset).where(MediaAsset.video_id == video.id,
            MediaAsset.class_ == "ORIGINAL", MediaAsset.status == "ACTIVE"))
        if original is None:
            raise ValueError("原始视频不可用")
        root = Path(settings.tmp_dir) / "cache" / original.content_hash
        root.mkdir(parents=True, exist_ok=True)
        local = root / "original"
        if not local.exists():
            temp = root / f".quiz-{g.id}"
            s3.download_file(original.object_key, str(temp))
            temp.replace(local)
        # Visual mode returns trajectory-supported contacts; legacy mode returns all acoustic impacts.
        _, impacts = detect_rallies_for_video(local, [(0, video.duration_ms)], work_dir=root / "work",
            ffmpeg_bin=settings.ffmpeg_bin, ffprobe_bin=settings.ffprobe_bin, blurball_config=vision_config(settings))
        candidates = detect_serve_candidates(impacts, video.duration_ms)
        windows = practice_features(local, root / "work", impacts, video.duration_ms, settings)
        known = set(session.scalars(select(PracticeWindow.start_ms).where(
            PracticeWindow.video_id == video.id, PracticeWindow.timeline_id == g.timeline_id,
            PracticeWindow.feature_version == FEATURE_VERSION)))
        for w in windows:
            if w["start_ms"] not in known:
                session.add(PracticeWindow(video_id=video.id, timeline_id=g.timeline_id,
                    feature_version=FEATURE_VERSION, **w))
        session.refresh(video)
        if video.deleted_at is not None:
            raise ValueError("视频已不可用")
        existing = set(session.scalars(select(QuizItem.candidate_index).where(QuizItem.generation_id == g.id)))
        for index, c in enumerate(candidates[:300]):
            if index in existing or c.start_ms >= c.contact_ms:
                continue
            session.add(QuizItem(video_id=video.id, timeline_id=g.timeline_id,
                generation_id=g.id, candidate_index=index, **asdict(c),
                provenance={"source": "MODEL", "source_id": DETECTOR_VERSION,
                    "confidence": None, "boundary_confirmed": False}))
        g.status, g.error = "READY", None
        g.limitations = ["练习类型结合画面运动与击球节奏，用本视频已复核片段训练；小样本分类尚未验证泛化准确率。",
            "声音只用于提议触球位置；发接发分类不代表每个候选都是发球。仍需确认动作、边界和接球前暂停点。"]
        if not any(w["features"] for w in windows):
            g.limitations += ["画面特征不可用，自动类型判断保持不确定；可回看并人工确认。"]
        if not impacts:
            g.limitations += ["未提取到击球信号，可手动圈定片段。"]
        if len(candidates) > 300:
            g.limitations += [f"共找到 {len(candidates)} 条候选，本次保留前 300 条；其余可手动补充。"]
        session.commit()
    except Exception:
        log.exception("quiz generation failed: %s", generation_id)
        session.rollback()
        g = session.get(QuizGeneration, generation_id)
        g.status, g.error = "FAILED", "候选生成失败，可重试或手动圈定片段。"
        session.commit()
    return "done"
