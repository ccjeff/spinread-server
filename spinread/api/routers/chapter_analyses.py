"""User-initiated, scoped visual assessments; independent of analysis facts."""
import hashlib
import json
from datetime import timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.api.deps import get_current_user, get_db, get_owned_video
from spinread.api.errors import ApiError, not_found
from spinread.api.routers.training_annotations import latest
from spinread.config import get_settings
from spinread.core.access import viewable_video
from spinread.core.ids import new_id
from spinread.core.models import ChapterAnalysis, MediaAsset, Timeline, TimelineActivePointer, User, Video, utcnow
from spinread.core.queue import enqueue
from spinread.product.chapter_analysis import MODEL, VERSION, TERMINAL, forget_key, put_key, AnalysisError

router = APIRouter(prefix="/api/videos", tags=["chapter analysis"])


class AnalysisRequest(BaseModel):
    chapter_id: str = Field(min_length=1, max_length=150)
    title: str = Field(min_length=1, max_length=100)
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    timeline_version: int = Field(ge=1, strict=True)
    annotation_version: int = Field(ge=0, strict=True)
    annotation_id: str | None = Field(default=None, max_length=100)
    target: Literal['NEAR', 'FAR']
    focus: str = Field(default='', max_length=500)
    budget_micro_usd: int = Field(default=150000, ge=100000, le=1000000, strict=True)
    consent: Literal[True]
    force: bool = False

    @model_validator(mode='after')
    def bounds(self):
        if self.end_ms - self.start_ms < 1000:
            raise ValueError('请选择至少一秒的训练区间')
        return self


def expire(row):
    if row.status not in TERMINAL and row.updated_at.replace(tzinfo=timezone.utc) < utcnow() - timedelta(minutes=15):
        row.status = 'FAILED'
        row.error = {'code': 'EXPIRED', 'message': '任务已超时或服务中断，请重新提交分析'}
        row.updated_at = utcnow()
        forget_key(row.id)


def output(row, db):
    expire(row)
    pointer = db.get(TimelineActivePointer, row.video_id)
    timeline = db.get(Timeline, pointer.timeline_id) if pointer else None
    annotation = latest(db, row.video_id)
    video = db.get(Video, row.video_id)
    asset = db.get(MediaAsset, row.snapshot['media_asset_id'])
    stale = (not asset or asset.status != 'ACTIVE' or asset.content_hash != row.snapshot['media_hash']
             or video.context_version != row.snapshot.get('context_version', video.context_version)
             or not timeline or timeline.version != row.snapshot['timeline_version']
             or (annotation.version if annotation else 0) != row.snapshot['annotation_version'])
    return {k: getattr(row, k) for k in ('id', 'video_id', 'chapter_id', 'start_ms', 'end_ms', 'status', 'snapshot',
        'result', 'manifest', 'coverage', 'usage', 'error', 'budget_micro_usd', 'created_at', 'updated_at')} | {'stale': bool(stale)}


@router.get('/{video_id}/chapter-analyses')
def list_analyses(video_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    viewable_video(video_id, db, user)
    rows = db.scalars(select(ChapterAnalysis).where(ChapterAnalysis.video_id == video_id).order_by(ChapterAnalysis.created_at.desc()).limit(50)).all()
    return {'items': [output(row, db) for row in rows]}


@router.get('/{video_id}/chapter-analyses/{analysis_id}')
def read_analysis(video_id: str, analysis_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    viewable_video(video_id, db, user)
    row = db.get(ChapterAnalysis, analysis_id)
    if not row or row.video_id != video_id:
        raise not_found()
    return output(row, db)


@router.post('/{video_id}/chapter-analyses', status_code=202)
def create_analysis(video_id: str, body: AnalysisRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user),
                    idempotency_key: str = Header(min_length=8, max_length=100), x_openai_key: str = Header(default='')):
    video = get_owned_video(video_id, db, user)
    # Serialize same-video submissions, including first submission.
    db.scalar(select(Video).where(Video.id == video_id).with_for_update())
    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    previous = db.scalar(select(ChapterAnalysis).where(ChapterAnalysis.video_id == video_id,
        ChapterAnalysis.author_id == user.id, ChapterAnalysis.request_key == idempotency_key))
    if previous:
        if previous.request_hash != digest:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '同一次请求的内容发生变化，请重新提交')
        return output(previous, db)
    if not video.duration_ms or body.end_ms > video.duration_ms:
        raise ApiError(422, 'INVALID_RANGE', '分析区间超出视频范围')
    pointer = db.get(TimelineActivePointer, video_id)
    timeline = db.get(Timeline, pointer.timeline_id) if pointer else None
    annotation = latest(db, video_id)
    if not timeline or timeline.version != body.timeline_version or (annotation.version if annotation else 0) != body.annotation_version:
        raise ApiError(409, 'ANALYSIS_SOURCE_CHANGED', '训练标注或时间线已更新，请刷新后重试')
    segment = None
    if body.annotation_id:
        segment = next((s for s in (annotation.segments if annotation else []) if s['id'] == body.annotation_id), None)
        if not segment or (segment['start_ms'], segment['end_ms']) != (body.start_ms, body.end_ms):
            raise ApiError(409, 'ANALYSIS_SOURCE_CHANGED', '人工训练区间已更新，请刷新后重试')
    asset = db.scalar(select(MediaAsset).where(MediaAsset.video_id == video_id, MediaAsset.class_ == 'PROXY', MediaAsset.status == 'ACTIVE').order_by(MediaAsset.media_version.desc()).limit(1))
    if not asset:
        raise ApiError(409, 'MEDIA_UNAVAILABLE', '视频尚未准备完成')
    snapshot = {'title': body.title, 'target': body.target, 'focus': body.focus, 'start_ms': body.start_ms, 'end_ms': body.end_ms,
        'timeline_version': timeline.version, 'annotation_version': annotation.version if annotation else 0,
        'annotation': segment, 'training_context': video.training_context, 'context_version': video.context_version, 'media_asset_id': asset.id, 'media_hash': asset.content_hash,
        'model': MODEL, 'strategy_version': VERSION, 'consent': {'provider': 'OpenAI', 'scope': 'SAMPLED_FRAMES_ONLY', 'actor_id': user.id}}
    cache_hash = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
    rows = db.scalars(select(ChapterAnalysis).where(ChapterAnalysis.video_id == video_id).order_by(ChapterAnalysis.created_at.desc())).all()
    for row in rows:
        expire(row)
        if row.status not in TERMINAL:
            if row.cache_hash == cache_hash:
                return output(row, db)
            raise ApiError(409, 'ANALYSIS_BUSY', '此视频已有分析进行中，请等待完成')
        if not body.force and row.cache_hash == cache_hash and row.status == 'SUCCEEDED':
            return output(row, db)
    if not get_settings().embed_worker:
        raise ApiError(503, 'WORKER_REQUIRED', '此版本的临时密钥功能需要单进程内嵌 worker')
    key = x_openai_key.strip()
    if not key or len(key) > 1024 or any(c.isspace() for c in key):
        raise ApiError(422, 'KEY_REQUIRED', '请输入本次分析使用的 OpenAI API key')
    row = ChapterAnalysis(id=new_id('cva'), video_id=video_id, author_id=user.id, chapter_id=body.chapter_id,
        start_ms=body.start_ms, end_ms=body.end_ms, request_key=idempotency_key, request_hash=digest,
        cache_hash=cache_hash, snapshot=snapshot, status='QUEUED', budget_micro_usd=body.budget_micro_usd,
        result=None, manifest=[], coverage=[], usage=[], error=None)
    try:
        put_key(row.id, key)
        db.add(row)
        db.flush()
        enqueue(db, 'CHAPTER_ANALYSIS', {'analysis_id': row.id}, max_attempts=1, priority=40)
        db.flush()
    except AnalysisError as exc:
        raise ApiError(429, exc.code, exc.public_message) from None
    except Exception:
        forget_key(row.id)
        raise
    return output(row, db)


@router.post('/{video_id}/chapter-analyses/{analysis_id}/cancel')
def cancel_analysis(video_id: str, analysis_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    get_owned_video(video_id, db, user)
    row = db.get(ChapterAnalysis, analysis_id)
    if not row or row.video_id != video_id:
        raise not_found()
    if row.status not in TERMINAL:
        row.status, row.updated_at = 'CANCELLED', utcnow()
        forget_key(row.id)
    return output(row, db)
