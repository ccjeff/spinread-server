"""Async, repeatable candidate preparation using the full video's onsets."""
import logging
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import select
from pingpong_training.analysis.rally import detect_rallies_for_video
from pingpong_training.analysis.serve import DETECTOR_VERSION, detect_serve_candidates
from spinread.core.models import MediaAsset, QuizGeneration, QuizItem, Video

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
        # The API also returns ALL impacts even when no rally passes its filters.
        _, impacts = detect_rallies_for_video(local, [(0, video.duration_ms)], work_dir=root / "work",
            ffmpeg_bin=settings.ffmpeg_bin, ffprobe_bin=settings.ffprobe_bin)
        candidates = detect_serve_candidates(impacts, video.duration_ms)
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
        g.limitations = ["候选基于声音与静默间隔，可能漏检或误检；不是已确认的发球。", "暂停点为建议位置，请回看片段并确认在接球前暂停；参考答案未核实。"]
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
