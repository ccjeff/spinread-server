"""User-initiated, scoped visual assessments; independent of analysis facts."""
import hashlib
import json
from copy import deepcopy
from datetime import timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field, model_validator
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
from spinread.product.chapter_analysis import MODEL, VERSION, TERMINAL, configured_key, AnalysisError
from spinread.product.chapter_contract import build_input, intent_signature

router = APIRouter(prefix="/api/videos", tags=["chapter analysis"])


class ComparisonSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    video_id: str = Field(min_length=1, max_length=150)
    annotation_id: str = Field(min_length=1, max_length=100)
    annotation_version: int = Field(ge=1)
    same_player: Literal[True]


class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    chapter_id: str = Field(min_length=1, max_length=150)
    title: str = Field(min_length=1, max_length=100)
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    timeline_version: int = Field(ge=1, strict=True)
    annotation_version: int = Field(ge=0, strict=True)
    annotation_id: str | None = Field(default=None, max_length=100)
    target: Literal['NEAR', 'FAR']
    focus: str = Field(default='', max_length=500)
    comparison: ComparisonSelection | None = None
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


def output(row, db, user):
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
    result = deepcopy(row.result)
    if result:
        result.pop('limitations', None)  # Internal input/coverage caveats are not product content.
    source_unavailable = False
    previous = row.snapshot.get('previous')
    comparison_source = None
    if previous:
        scope = previous['input']['scope']
        try:
            prior_video = viewable_video(scope['video_id'], db, user)
            prior_annotation = latest(db, prior_video.id)
            prior_asset = db.get(MediaAsset, previous['media_asset_id'])
            prior_pointer = db.get(TimelineActivePointer, prior_video.id)
            prior_timeline = db.get(Timeline, prior_pointer.timeline_id) if prior_pointer else None
            stale = stale or (not prior_annotation or prior_annotation.version != scope['annotation_version']
                or not prior_asset or prior_asset.status != 'ACTIVE' or prior_asset.content_hash != previous['media_hash']
                or not prior_timeline or prior_timeline.version != scope['timeline_version'])
            comparison_source = {**scope, 'title': previous['title'], 'filename': prior_video.filename,
                'target': previous['input']['target']['position']}
        except ApiError:
            # Sharing the current video never implicitly shares another video.
            result = None
            source_unavailable = True
            stale = True
    public_manifest = [{k: f[k] for k in ('id', 'timestamp_ms', 'video_id', 'role') if k in f}
        for f in row.manifest if f.get('role') != 'PREVIOUS' or comparison_source]
    return {k: getattr(row, k) for k in ('id', 'video_id', 'chapter_id', 'start_ms', 'end_ms', 'status',
        'created_at', 'updated_at')} | {'stale': bool(stale), 'result': result, 'manifest': public_manifest,
        'snapshot': {k: row.snapshot[k] for k in ('title', 'target', 'focus')},
        'comparison_source': comparison_source,
        'error': ({'message': '对比引用的历史训练已不可访问，请重新分析本段。'} if source_unavailable
                  else {'message': '分析未完成，请稍后重试'} if row.error else None)}


def proxy(db, video_id):
    return db.scalar(select(MediaAsset).where(MediaAsset.video_id == video_id,
        MediaAsset.class_ == 'PROXY', MediaAsset.status == 'ACTIVE').order_by(MediaAsset.media_version.desc()).limit(1))


def input_for(video, segment, timeline, revision, target, focus, manifest_id, chapter_id=None):
    return build_input(video_id=video.id, chapter_id=chapter_id or segment['id'], start_ms=segment['start_ms'],
        end_ms=segment['end_ms'], timeline_version=timeline.version, annotation_version=revision.version if revision else 0,
        target=target, annotation=segment if revision else None, focus=focus, manifest_id=manifest_id).model_dump()


@router.get('/{video_id}/chapter-comparison-candidates')
def comparison_candidates(video_id: str, annotation_id: str = '', target: str = '',
                          db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    video = get_owned_video(video_id, db, user)
    revision = latest(db, video.id)
    segment = next((s for s in revision.segments if s['id'] == annotation_id), None) if revision else None
    signature = intent_signature(segment)
    if not signature or segment['target'] != target:
        return {'items': [], 'reason': '请先为目标球员标注具体动作，才能匹配同类历史训练。'}
    items = []
    videos = db.scalars(select(Video).where(Video.owner_id == user.id, Video.id != video.id,
        Video.deleted_at.is_(None)).order_by(Video.created_at.desc())).all()
    for prior in videos:
        annotation = latest(db, prior.id)
        if not annotation or not proxy(db, prior.id) or not db.get(TimelineActivePointer, prior.id):
            continue
        for s in annotation.segments:
            if intent_signature(s) == signature and prior.duration_ms and 0 <= s['start_ms'] < s['end_ms'] <= prior.duration_ms:
                items.append({'video_id': prior.id, 'annotation_id': s['id'], 'annotation_version': annotation.version,
                    'title': s['title'], 'filename': prior.filename, 'start_ms': s['start_ms'], 'end_ms': s['end_ms'],
                    'target': s['target'], 'recorded_at': prior.recorded_at})
    return {'items': items, 'reason': '' if items else '还没有动作与供球方式相同的其他训练，标注后可在这里选择对比。'}


@router.get('/{video_id}/chapter-analyses')
def list_analyses(video_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    viewable_video(video_id, db, user)
    rows = db.scalars(select(ChapterAnalysis).where(ChapterAnalysis.video_id == video_id).order_by(ChapterAnalysis.created_at.desc()).limit(50)).all()
    return {'items': [output(row, db, user) for row in rows]}


@router.get('/{video_id}/chapter-analyses/{analysis_id}')
def read_analysis(video_id: str, analysis_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    viewable_video(video_id, db, user)
    row = db.get(ChapterAnalysis, analysis_id)
    if not row or row.video_id != video_id:
        raise not_found()
    return output(row, db, user)


@router.post('/{video_id}/chapter-analyses', status_code=202)
def create_analysis(video_id: str, body: AnalysisRequest, db: Session = Depends(get_db), user: User = Depends(get_current_user),
                    idempotency_key: str = Header(min_length=8, max_length=100)):
    video = get_owned_video(video_id, db, user)
    # Serialize same-video submissions, including first submission.
    db.scalar(select(Video).where(Video.id == video_id).with_for_update())
    digest = hashlib.sha256(json.dumps(body.model_dump(), sort_keys=True).encode()).hexdigest()
    previous = db.scalar(select(ChapterAnalysis).where(ChapterAnalysis.video_id == video_id,
        ChapterAnalysis.author_id == user.id, ChapterAnalysis.request_key == idempotency_key))
    if previous:
        if previous.request_hash != digest:
            raise ApiError(409, 'IDEMPOTENCY_CONFLICT', '同一次请求的内容发生变化，请重新提交')
        return output(previous, db, user)
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
    asset = proxy(db, video_id)
    if not asset:
        raise ApiError(409, 'MEDIA_UNAVAILABLE', '视频尚未准备完成')
    analysis_id = new_id('cva')
    contract = build_input(video_id=video_id, chapter_id=body.chapter_id, start_ms=body.start_ms, end_ms=body.end_ms,
        timeline_version=timeline.version, annotation_version=annotation.version if annotation else 0,
        target=body.target, annotation=segment, focus=body.focus, manifest_id=f'{analysis_id}:samples').model_dump()
    previous = None
    if body.comparison:
        selection = body.comparison
        prior_video = get_owned_video(selection.video_id, db, user)
        prior_revision = latest(db, prior_video.id)
        prior_segment = next((s for s in prior_revision.segments if s['id'] == selection.annotation_id), None) if prior_revision else None
        prior_pointer = db.get(TimelineActivePointer, prior_video.id)
        prior_timeline = db.get(Timeline, prior_pointer.timeline_id) if prior_pointer else None
        prior_asset = proxy(db, prior_video.id)
        if (prior_video.id == video_id or not prior_revision or prior_revision.version != selection.annotation_version
            or not prior_timeline or not prior_asset or not prior_segment or not prior_video.duration_ms
            or not 0 <= prior_segment['start_ms'] < prior_segment['end_ms'] <= prior_video.duration_ms
            or not intent_signature(segment) or segment['target'] != body.target
            or intent_signature(segment) != intent_signature(prior_segment)):
            raise ApiError(409, 'COMPARISON_CHANGED', '所选历史训练已更新或动作不匹配，请重新选择')
        prior_input = input_for(prior_video, prior_segment, prior_timeline, prior_revision,
            prior_segment['target'], body.focus, f'{analysis_id}:samples')
        prior_input['known_limitations'].append('同一球员由用户选择确认；不能凭近端或远端位置验证身份。')
        previous = {'input': prior_input, 'title': prior_segment['title'], 'media_asset_id': prior_asset.id,
            'media_hash': prior_asset.content_hash, 'same_player_source': 'USER'}
    snapshot = {'title': body.title, 'target': body.target, 'focus': body.focus, 'start_ms': body.start_ms, 'end_ms': body.end_ms,
        'timeline_version': timeline.version, 'annotation_version': annotation.version if annotation else 0,
        'annotation': segment, 'training_context': video.training_context, 'context_version': video.context_version, 'media_asset_id': asset.id, 'media_hash': asset.content_hash,
        'model': MODEL, 'strategy_version': VERSION, 'input': contract, 'previous': previous,
        'requested_by': user.id}
    cache_snapshot = deepcopy(snapshot)
    cache_snapshot['input']['sample_manifest_id'] = ''
    if previous:
        cache_snapshot['previous']['input']['sample_manifest_id'] = ''
    cache_hash = hashlib.sha256(json.dumps(cache_snapshot, sort_keys=True).encode()).hexdigest()
    rows = db.scalars(select(ChapterAnalysis).where(ChapterAnalysis.video_id == video_id).order_by(ChapterAnalysis.created_at.desc())).all()
    for row in rows:
        expire(row)
        if row.status not in TERMINAL:
            if row.cache_hash == cache_hash:
                return output(row, db, user)
            raise ApiError(409, 'ANALYSIS_BUSY', '此视频已有分析进行中，请等待完成')
        if not body.force and row.cache_hash == cache_hash and row.status == 'SUCCEEDED':
            return output(row, db, user)
    try:
        configured_key(get_settings())
    except AnalysisError as exc:
        raise ApiError(503, exc.code, exc.public_message) from None
    row = ChapterAnalysis(id=analysis_id, video_id=video_id, author_id=user.id, chapter_id=body.chapter_id,
        start_ms=body.start_ms, end_ms=body.end_ms, request_key=idempotency_key, request_hash=digest,
        cache_hash=cache_hash, snapshot=snapshot, status='QUEUED', budget_micro_usd=150000,
        result=None, manifest=[], coverage=[], usage=[], error=None)
    db.add(row)
    db.flush()
    enqueue(db, 'CHAPTER_ANALYSIS', {'analysis_id': row.id}, max_attempts=1, priority=40)
    db.flush()
    return output(row, db, user)


@router.post('/{video_id}/chapter-analyses/{analysis_id}/cancel')
def cancel_analysis(video_id: str, analysis_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    get_owned_video(video_id, db, user)
    row = db.get(ChapterAnalysis, analysis_id)
    if not row or row.video_id != video_id:
        raise not_found()
    if row.status not in TERMINAL:
        row.status, row.updated_at = 'CANCELLED', utcnow()
    return output(row, db, user)
