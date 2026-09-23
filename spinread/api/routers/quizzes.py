"""Private observation quizzes; approval confirms clip boundaries, not labels."""
from __future__ import annotations

import hashlib
import json
from typing import Literal

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from pingpong_training.analysis.serve import DETECTOR_VERSION
from spinread.api.deps import get_current_user, get_db, get_owned_video
from spinread.api.errors import ApiError, not_found
from spinread.core.models import QuizAttempt, QuizGeneration, QuizItem, Timeline, TimelineActivePointer, User, Video
from spinread.core.queue import enqueue

router = APIRouter(prefix="/api", tags=["quizzes"])


def conflict(message):
    return ApiError(409, "QUIZ_VERSION_CONFLICT", message)


def active(db, video_id):
    pointer = db.get(TimelineActivePointer, video_id)
    if pointer is None:
        raise ApiError(409, "NO_TIMELINE", "视频时间线尚未生成")
    return db.get(Timeline, pointer.timeline_id)


def lock_video(db, video_id):
    # Serialize generation, item revisions, and attempt retries per video.
    db.scalar(select(Video).where(Video.id == video_id).with_for_update())


def owned_item(db, user, item_id):
    item = db.get(QuizItem, item_id)
    if item is None:
        raise not_found("练习不存在")
    video = get_owned_video(item.video_id, db, user)
    lock_video(db, video.id)
    db.refresh(item)
    return item, video


def item_out(db, item, timeline_id, attempted=None):
    stale = item.timeline_id != timeline_id or not item.is_current
    return {
        "id": item.id, "video_id": item.video_id, "version": item.version,
        "start_ms": item.start_ms, "contact_ms": item.contact_ms,
        "pause_ms": item.pause_ms, "end_ms": item.end_ms,
        "approval": item.approval, "stale": stale,
        "provenance": item.provenance, "scorable": False,
        "attempted": item.id in (attempted or set()),
    }


def generation_out(g):
    return None if g is None else {"id": g.id, "status": g.status, "error": g.error, "limitations": g.limitations}


@router.get("/videos/{video_id}/quiz-items")
def list_items(video_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    video = get_owned_video(video_id, db, user)
    tl = active(db, video.id)
    items = db.scalars(select(QuizItem).where(QuizItem.video_id == video.id, QuizItem.is_current.is_(True)).order_by(QuizItem.start_ms)).all()
    attempted = set(db.scalars(select(QuizAttempt.quiz_item_id).join(QuizItem).where(QuizItem.video_id == video.id, QuizAttempt.user_id == user.id)))
    generation = db.scalar(select(QuizGeneration).where(QuizGeneration.video_id == video.id, QuizGeneration.timeline_id == tl.id, QuizGeneration.detector_version == DETECTOR_VERSION))
    return {"video_id": video.id, "filename": video.filename, "duration_ms": video.duration_ms,
            "timeline_version": tl.version, "generation": generation_out(generation),
            "items": [item_out(db, i, tl.id, attempted) for i in items]}


@router.post("/videos/{video_id}/quiz-candidates", status_code=202)
def generate(video_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    video = get_owned_video(video_id, db, user)
    lock_video(db, video.id)
    if video.state not in ("READY", "PARTIAL_READY"):
        raise ApiError(409, "VIDEO_NOT_READY", "请等待视频分析完成")
    tl = active(db, video.id)
    g = db.scalar(select(QuizGeneration).where(QuizGeneration.video_id == video.id, QuizGeneration.timeline_id == tl.id, QuizGeneration.detector_version == DETECTOR_VERSION))
    if g is None:
        g = QuizGeneration(video_id=video.id, timeline_id=tl.id, detector_version=DETECTOR_VERSION)
        db.add(g)
        db.flush()
        enqueue(db, "QUIZ_GENERATE", {"generation_id": g.id}, max_attempts=1, priority=50)
    elif g.status == "FAILED":
        g.status, g.error = "QUEUED", None
        enqueue(db, "QUIZ_GENERATE", {"generation_id": g.id}, max_attempts=1, priority=50)
    return generation_out(g)


class EditItem(BaseModel):
    timeline_version: int = Field(ge=1)
    base_version: int | None = Field(default=None, ge=1)
    start_ms: int = Field(ge=0)
    contact_ms: int = Field(ge=0)
    pause_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    approval: Literal["DRAFT", "APPROVED", "WITHDRAWN"] = "DRAFT"

    @model_validator(mode="after")
    def bounds(self):
        if not self.start_ms < self.contact_ms < self.pause_ms < self.end_ms:
            raise ValueError("需要 开始 < 发球触球 < 接球前暂停 < 结束")
        if self.end_ms - self.start_ms > 30_000:
            raise ValueError("单条发球练习最长 30 秒")
        return self


def save_item(db, user, video, body, old=None):
    tl = active(db, video.id)
    if tl.version != body.timeline_version:
        raise conflict("时间线已更新，请刷新后重新确认片段")
    if body.end_ms > (video.duration_ms or 0):
        raise ApiError(422, "INVALID_BOUNDS", "片段超出视频时长")
    if old and (not old.is_current or old.version != body.base_version):
        raise conflict("题目已更新，请刷新后重试")
    kwargs = {}
    if old:
        old.is_current = False
        kwargs = {"family_id": old.family_id, "version": old.version + 1}
    item = QuizItem(video_id=video.id, timeline_id=tl.id,
        start_ms=body.start_ms, contact_ms=body.contact_ms, pause_ms=body.pause_ms, end_ms=body.end_ms,
        approval=body.approval, provenance={"source": "USER", "source_id": user.id,
            "boundary_confirmed": body.approval == "APPROVED", "previous_item_id": old.id if old else None}, **kwargs)
    db.add(item)
    db.flush()
    return item_out(db, item, tl.id)


@router.post("/videos/{video_id}/quiz-items", status_code=201)
def create_item(video_id: str, body: EditItem, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    video = get_owned_video(video_id, db, user)
    lock_video(db, video.id)
    return save_item(db, user, video, body)


@router.post("/quiz-items/{item_id}/revisions", status_code=201)
def revise(item_id: str, body: EditItem, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    old, video = owned_item(db, user, item_id)
    return save_item(db, user, video, body, old)


@router.get("/quiz-sets/recommended")
def recommended(video_id: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if video_id:
        get_owned_video(video_id, db, user)
    has_attempt = select(QuizAttempt.id).where(QuizAttempt.quiz_item_id == QuizItem.id, QuizAttempt.user_id == user.id).exists()
    stmt = select(QuizItem).join(Video).join(TimelineActivePointer, TimelineActivePointer.video_id == Video.id).where(
        Video.owner_id == user.id, Video.deleted_at.is_(None), QuizItem.is_current.is_(True),
        QuizItem.approval == "APPROVED", QuizItem.timeline_id == TimelineActivePointer.timeline_id)
    if video_id:
        stmt = stmt.where(Video.id == video_id)
    items = db.scalars(stmt.order_by(has_attempt.asc(), QuizItem.created_at.desc(), QuizItem.id).limit(200)).all()
    attempted = set(db.scalars(select(QuizAttempt.quiz_item_id).where(QuizAttempt.user_id == user.id)))
    return {"items": [item_out(db, i, i.timeline_id, attempted) for i in items]}


class Answer(BaseModel):
    spin: Literal["UNKNOWN", "TOPSPIN", "BACKSPIN", "SIDESPIN", "SIDE_TOP", "SIDE_BACK", "NO_SPIN"]
    length: Literal["UNKNOWN", "SHORT", "HALF_LONG", "LONG"]
    receive: Literal["UNKNOWN", "PUSH", "FLICK", "TOPSPIN", "BLOCK", "CHOP"]
    note: str = Field(default="", max_length=2000)


class AttemptIn(BaseModel):
    item_version: int = Field(ge=1)
    answer: Answer
    confidence: int = Field(ge=1, le=5)
    elapsed_ms: int = Field(ge=0, le=86_400_000)


def attempt_out(a):
    return {"id": a.id, "quiz_item_id": a.quiz_item_id, "answer": a.answer,
        "confidence": a.confidence, "elapsed_ms": a.elapsed_ms, "scored": a.scored,
        "feedback_version": a.feedback_version, "created_at": a.created_at,
        "feedback": "判断已记录。观看真实后续，与自己的接法比较；参考答案尚未核实，本题不计分。可将疑问留给教练讨论。"}


@router.post("/quiz-items/{item_id}/attempts", status_code=201)
def submit_attempt(item_id: str, body: AttemptIn, idempotency_key: str = Header(min_length=8, max_length=100),
                   db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item, video = owned_item(db, user, item_id)
    db.scalar(select(User).where(User.id == user.id).with_for_update())
    digest = hashlib.sha256(json.dumps({"item": item_id, **body.model_dump()}, sort_keys=True).encode()).hexdigest()
    existing = db.scalar(select(QuizAttempt).where(QuizAttempt.user_id == user.id, QuizAttempt.request_id == idempotency_key))
    if existing:
        if existing.request_hash != digest:
            raise ApiError(409, "IDEMPOTENCY_KEY_REUSE", "重复请求的内容不一致")
        return attempt_out(existing)
    tl = active(db, video.id)
    if not item.is_current or item.timeline_id != tl.id or item.version != body.item_version:
        raise conflict("题目或时间线已更新，请回到片段列表重新确认")
    if item.approval != "APPROVED":
        raise ApiError(409, "QUIZ_NOT_APPROVED", "请先确认发球片段的边界和暂停点")
    a = QuizAttempt(quiz_item_id=item.id, user_id=user.id, request_id=idempotency_key,
        request_hash=digest, answer=body.answer.model_dump(), confidence=body.confidence,
        elapsed_ms=body.elapsed_ms, scored=False)
    db.add(a)
    db.flush()
    return attempt_out(a)


@router.get("/quiz-items/{item_id}/attempts")
def attempts(item_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    item, video = owned_item(db, user, item_id)
    # Include prior revisions for personal/coach discussion without rewriting history.
    rows = db.scalars(select(QuizAttempt).join(QuizItem).where(QuizItem.family_id == item.family_id,
        QuizAttempt.user_id == user.id).order_by(QuizAttempt.created_at.desc()).limit(50)).all()
    return [attempt_out(a) for a in rows]
