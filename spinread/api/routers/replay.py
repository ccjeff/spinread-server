"""Evidence replay bound to the active timeline, with inherited video access."""
from __future__ import annotations
import json
from typing import Literal
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from pingpong_training.analysis.replay import replay_evidence, replay_window
from pingpong_training.analysis.placement import placement_candidates, placement_statistics, table_homography
from spinread.api.deps import get_current_user, get_db, get_s3, get_owned_video
from spinread.api.errors import ApiError, not_found
from spinread.core.access import viewable_video
from spinread.core.models import Artifact, ReplayAnalysis, ReplayReview, Timeline, TimelineActivePointer, TimelineItem, User, utcnow

router = APIRouter(prefix='/api/videos', tags=['replay'])


def current(db, video_id, version, lock=False):
    query = select(TimelineActivePointer).where(TimelineActivePointer.video_id == video_id)
    pointer = db.scalar(query.with_for_update() if lock else query)
    timeline = db.get(Timeline, pointer.timeline_id) if pointer else None
    if timeline is None:
        raise not_found('Timeline not available')
    if timeline.version != version:
        raise ApiError(409, 'TIMELINE_VERSION_CONFLICT', '时间线已更新，请刷新复盘。')
    rows = db.scalars(select(TimelineItem).where(TimelineItem.timeline_id == timeline.id,
        TimelineItem.status == 'ACTIVE').order_by(TimelineItem.start_ms)).all()
    return rows


def review_data(db, video, version):
    row = db.get(ReplayReview, video.id)
    pointer = db.get(TimelineActivePointer, video.id)
    analysis = db.scalar(select(ReplayAnalysis).where(ReplayAnalysis.video_id == video.id,
        ReplayAnalysis.timeline_id == pointer.timeline_id if pointer else False,
        ReplayAnalysis.state.in_(['READY', 'PARTIAL_READY']), ReplayAnalysis.artifact_id.is_not(None))
        .order_by(ReplayAnalysis.created_at.desc(), ReplayAnalysis.id.desc()).limit(1))
    analysis_id = analysis.artifact_id if analysis else None
    stale = bool(row and (row.timeline_version != version or row.data.get('analysis_artifact_id') != analysis_id))
    return {'version': row.version if row else 0, 'timeline_version': version,
            'stale': stale, 'analysis_artifact_id': analysis_id,
            'corners': row.data.get('corners') if row else None,
            'calibration_start_ms': row.data.get('calibration_start_ms', 0) if row else 0,
            'calibration_end_ms': row.data.get('calibration_end_ms', video.duration_ms) if row else video.duration_ms,
            'reviews': row.data.get('reviews', []) if row and not stale else []}


def evidence_for(db, s3, video, rally, cache):
    from spinread.product.replay_analysis import analysis_evidence, merge_evidence
    enriched = analysis_evidence(db, s3, video, rally.timeline_id, cache)
    aid = (rally.attributes or {}).get('replay_artifact_id')
    if not aid:
        return (enriched, True) if enriched is not None else (replay_evidence(None, [], video.duration_ms), False)
    if aid in cache:
        return merge_evidence(cache[aid], enriched) if enriched is not None else cache[aid], True
    artifact = db.get(Artifact, aid)
    if artifact is None or artifact.video_id != video.id or artifact.stage != 'EVENTS':
        raise not_found('Replay evidence unavailable')
    payload = json.loads(s3.get_bytes(artifact.object_key))
    if payload.get('video_id') != video.id or not payload.get('replay'):
        raise not_found('Replay evidence unavailable')
    cache[aid] = payload['replay']
    return merge_evidence(cache[aid], enriched) if enriched is not None else cache[aid], True


def rally_evidence(db, s3, video, rally, rows, review, cache):
    evidence, available = evidence_for(db, s3, video, rally, cache)
    hits = [r.start_ms for r in rows if r.parent_id == rally.id and r.type == 'HIT_CANDIDATE']
    window = replay_window(evidence, rally.start_ms, rally.end_ms, hits)
    candidates = [dict(c) for c in window.get('placements', [])]
    if review['corners']:
        local = placement_candidates(window['frames'], window['contacts'], review['corners'])
        candidates.extend(c for c in local
            if review['calibration_start_ms'] <= c['time_ms'] < review['calibration_end_ms']
            and not any(abs(c['time_ms'] - auto['time_ms']) < 120 for auto in candidates))
    by_time = {r['time_ms']: r for r in review['reviews'] if r['rally_id'] == rally.id}
    for c in candidates:
        c['rally_id'] = rally.id
        if c['time_ms'] in by_time:
            c.update({k: by_time[c['time_ms']][k] for k in ('status','phase')})
    return evidence, hits, candidates, available


def statistics(candidates):
    # A model estimate is never promoted into the human-confirmed denominator.
    known = lambda c: (c.get('table_xy') is not None and c.get('depth') in ('SHORT','HALF_LONG','LONG')
                       and c.get('lateral') in ('LEFT','CENTER','RIGHT'))
    complete = [c for c in candidates if known(c)]
    result = placement_statistics(complete)
    result['candidate_count'] = sum(c['status'] == 'CANDIDATE' for c in candidates)
    result['unknown_count'] = sum(not known(c) for c in candidates)
    estimates = [c for c in candidates if c.get('basis') == 'PINGPONG3D_MODEL_ESTIMATE' and c['status'] != 'REJECTED']
    phases = {}
    for phase in ('SERVE','RECEIVE','RALLY'):
        selected = [c for c in estimates if c.get('phase') == phase]
        grid = [[0]*3 for _ in range(3)]
        for c in selected:
            if known(c):
                grid[('SHORT','HALF_LONG','LONG').index(c['depth'])][('LEFT','CENTER','RIGHT').index(c['lateral'])] += 1
        phases[phase] = {'sample_count':sum(known(c) for c in selected),
                         'unknown_count':sum(not known(c) for c in selected), 'grid':grid}
    result['model_estimates'] = {'sample_count':sum(known(c) for c in estimates),
                                'unknown_count':sum(not known(c) for c in estimates), 'phases':phases}
    return result


@router.get('/{video_id}/rallies/{rally_id}/replay')
def get_replay(video_id: str, rally_id: str, timeline_version: int,
               start_ms: int | None = Query(None, ge=0), end_ms: int | None = Query(None, gt=0), db: Session = Depends(get_db),
               user: User = Depends(get_current_user), s3=Depends(get_s3)):
    video = viewable_video(video_id, db, user)
    rows = current(db, video_id, timeline_version)
    rally = next((r for r in rows if r.id == rally_id and r.type == 'RALLY'), None)
    if rally is None:
        raise not_found('Active rally not found')
    review = review_data(db, video, timeline_version)
    evidence, hits, candidates, available = rally_evidence(db,s3,video,rally,rows,review,{})
    start = rally.start_ms if start_ms is None else start_ms
    if not rally.start_ms <= start < rally.end_ms:
        raise ApiError(422,'INVALID_REPLAY_RANGE','复盘起点必须在当前回合内。')
    end = min(rally.end_ms, start + 30000) if end_ms is None else end_ms
    if not start < end <= min(rally.end_ms, start + 30000):
        raise ApiError(422,'INVALID_REPLAY_RANGE','复盘区间必须在当前回合内且不超过 30 秒。')
    result = replay_window(evidence,start,end,hits)
    pose = evidence.get('pose')
    result['pose'] = ({**{k:v for k,v in pose.items() if k!='frames'},
                       'frames':[f for f in pose['frames'] if start<=f['time_ms']<end]} if pose else None)
    return {**result, 'video_id':video_id, 'rally_id':rally_id, 'timeline_version':timeline_version,
            'evidence_available':available, 'rally_start_ms':rally.start_ms,'rally_end_ms':rally.end_ms,
            'next_start_ms':end if end<rally.end_ms else None,
            'placements':[c for c in candidates if start<=c['time_ms']<end],
            'statistics':statistics(candidates), 'review':review}


@router.get('/{video_id}/replay-statistics')
def get_statistics(video_id: str, timeline_version: int, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user), s3=Depends(get_s3)):
    video=viewable_video(video_id,db,user);rows=current(db,video_id,timeline_version)
    review=review_data(db,video,timeline_version);cache={};candidates=[]
    for rally in rows:
        if rally.type=='RALLY':
            candidates.extend(rally_evidence(db,s3,video,rally,rows,review,cache)[2])
    return {'timeline_version':timeline_version,'review_version':review['version'],
            'stale':review['stale'],**statistics(candidates)}


class LandingReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rally_id: str
    time_ms: int = Field(ge=0)
    phase: Literal['SERVE','RECEIVE','RALLY','UNKNOWN']
    status: Literal['CONFIRMED','REJECTED']


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_version: int = Field(ge=0)
    timeline_version: int = Field(ge=1)
    analysis_artifact_id: str | None = None
    corners: list[tuple[float,float]] | None = Field(default=None,min_length=4,max_length=4)
    calibration_start_ms: int = Field(ge=0)
    calibration_end_ms: int = Field(gt=0)
    fixed_camera: Literal[True] | None = None
    reviews: list[LandingReview] = Field(default_factory=list,max_length=2000)


@router.put('/{video_id}/replay-review')
def put_review(video_id: str, body: ReviewRequest, db: Session = Depends(get_db),
               user: User = Depends(get_current_user), s3=Depends(get_s3)):
    video=get_owned_video(video_id,db,user)
    # Serialize first review creation; also lock the active pointer against timeline edits.
    from spinread.core.models import Video
    db.scalar(select(Video).where(Video.id==video.id).with_for_update())
    rows=current(db,video_id,body.timeline_version,lock=True)
    previous=db.get(ReplayReview,video_id)
    if (previous.version if previous else 0)!=body.base_version:
        raise ApiError(409,'REPLAY_REVIEW_CONFLICT','复核记录已更新，请刷新后重试。')
    if body.analysis_artifact_id != review_data(db, video, body.timeline_version)['analysis_artifact_id']:
        raise ApiError(409,'REPLAY_EVIDENCE_CONFLICT','分析证据已更新，请刷新后重新复核。')
    if body.corners is not None:
        if body.fixed_camera is not True:
            raise ApiError(422,'INVALID_CALIBRATION','手动球台标定必须确认机位固定。')
        try:
            table_homography(body.corners)
        except ValueError as exc:
            raise ApiError(422,'INVALID_CALIBRATION',str(exc))
    if not 0<=body.calibration_start_ms<body.calibration_end_ms<=video.duration_ms:
        raise ApiError(422,'INVALID_CALIBRATION_RANGE','标定区间超出原视频。')
    data=body.model_dump(mode='json',exclude={'base_version','timeline_version','fixed_camera'})
    # Recalibration invalidates previous landing confirmations. Clients must
    # submit an empty review list when changing camera geometry or interval.
    if previous and body.reviews and any(previous.data.get(k)!=data[k] for k in
            ('corners','calibration_start_ms','calibration_end_ms')):
        raise ApiError(422,'CALIBRATION_CHANGED','重新标定后请重新确认落点。')
    available={};cache={}
    draft={**data,'reviews':[]}
    wanted={r.rally_id for r in body.reviews}
    for rally in rows:
        if rally.type=='RALLY' and rally.id in wanted:
            for c in rally_evidence(db,s3,video,rally,rows,draft,cache)[2]:
                available[(rally.id,c['time_ms'])]=c
    seen=set()
    for review in body.reviews:
        key=(review.rally_id,review.time_ms)
        if (key not in available or key in seen or (review.status=='CONFIRMED' and
                (review.phase=='UNKNOWN' or available[key].get('table_xy') is None
                 or available[key].get('depth') not in ('SHORT','HALF_LONG','LONG')
                 or available[key].get('lateral') not in ('LEFT','CENTER','RIGHT')))):
            raise ApiError(422,'INVALID_LANDING_REVIEW','请选择当前回合中有视觉依据的落点，并确认所属阶段。')
        seen.add(key)
    if previous is None:
        previous=ReplayReview(video_id=video_id,reviewer_id=user.id,version=0,timeline_version=body.timeline_version)
        db.add(previous)
    previous.version+=1;previous.timeline_version=body.timeline_version;previous.data=data
    previous.reviewer_id=user.id;previous.updated_at=utcnow();db.flush()
    return review_data(db,video,body.timeline_version)
