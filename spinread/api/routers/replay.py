"""Evidence replay bound to the active timeline, with inherited video access."""
from __future__ import annotations
import json
import hashlib
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


def calibration_windows(data, duration):
    """Read legacy calibration JSON without changing the stored record."""
    if 'calibrations' in data:
        return sorted([dict(c) for c in data['calibrations']], key=lambda c:(c['start_ms'],c['id']))
    if data.get('corners'):
        return [{'id':'legacy','corners':data['corners'],
                 'start_ms':data.get('calibration_start_ms',0),
                 'end_ms':data.get('calibration_end_ms',duration)}]
    return []


def calibration_legacy_fields(windows, duration):
    first = windows[0] if windows else None
    return {'corners':first['corners'] if first else None,
            'calibration_start_ms':first['start_ms'] if first else 0,
            'calibration_end_ms':first['end_ms'] if first else duration}


def review_data(db, video, version):
    row = db.get(ReplayReview, video.id)
    pointer = db.get(TimelineActivePointer, video.id)
    analysis = db.scalar(select(ReplayAnalysis).where(ReplayAnalysis.video_id == video.id,
        ReplayAnalysis.timeline_id == pointer.timeline_id if pointer else False,
        ReplayAnalysis.state.in_(['READY', 'PARTIAL_READY']), ReplayAnalysis.artifact_id.is_not(None))
        .order_by(ReplayAnalysis.created_at.desc(), ReplayAnalysis.id.desc()).limit(1))
    analysis_id = analysis.artifact_id if analysis else None
    compatible = {analysis_id}
    artifact = db.get(Artifact, analysis_id) if analysis_id else None
    if artifact and artifact.video_id == video.id and artifact.stage == 'REPLAY_ANALYSIS':
        compatible.update(v for v in (artifact.metrics or {}).get('placement_review_ancestor_ids', [])
                          if isinstance(v, str))
    stale = bool(row and (row.timeline_version != version or row.data.get('analysis_artifact_id') not in compatible))
    windows = calibration_windows(row.data if row else {},video.duration_ms)
    return {'version': row.version if row else 0, 'timeline_version': version,
            'stale': stale, 'analysis_artifact_id': analysis_id,
            'calibrations':windows, **calibration_legacy_fields(windows,video.duration_ms),
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
    evidence = calibrated_ball_evidence(evidence, review, video.duration_ms)
    hits = [r.start_ms for r in rows if r.parent_id == rally.id and r.type == 'HIT_CANDIDATE']
    window = replay_window(evidence, rally.start_ms, rally.end_ms, hits)
    candidates = [dict(c) for c in window.get('placements', [])]
    for calibration in calibration_windows(review,video.duration_ms):
        start,end = max(rally.start_ms,calibration['start_ms']),min(rally.end_ms,calibration['end_ms'])
        if start >= end:
            continue
        # The detector's full five-frame neighborhood and bracketing active hits
        # must stay inside one camera window, not merely the candidate's center.
        frames = [f for f in window['frames'] if start <= f[0] < end]
        contacts = [c for c in window['contacts'] if start <= c['time_ms'] < end]
        local = placement_candidates(frames,contacts,calibration['corners'])
        candidates.extend({**c,'calibration_id':calibration['id']} for c in local
            if not any(abs(c['time_ms'] - auto['time_ms']) < 120 for auto in candidates))
    by_time = {r['time_ms']: r for r in review['reviews'] if r['rally_id'] == rally.id}
    for c in candidates:
        c['rally_id'] = rally.id
        if c['time_ms'] in by_time:
            c.update({k: by_time[c['time_ms']][k] for k in ('status','phase')})
    return evidence, hits, candidates, available


def calibrated_ball_evidence(evidence, review, duration):
    """An edited camera must not display geometry fitted to its previous corners."""
    ball = evidence.get('ball_3d') or {}
    if ball.get('provider') != 'SPINREAD_BALLISTIC_EXPERIMENTAL':
        return evidence
    hashes = {c['id']:hashlib.sha256(json.dumps(c,sort_keys=True,separators=(',',':')).encode()).hexdigest()
              for c in calibration_windows(review,duration)}
    compatible = {c['calibration_id'] for c in ball.get('cameras',[])
                  if hashes.get(c['calibration_id']) == c.get('calibration_sha256')}
    segments = [s for s in ball.get('segments',[]) if s.get('calibration_id') in compatible]
    if len(segments) == len(ball.get('segments',[])):
        return evidence
    gaps=[]; cursor=0
    for segment in sorted(segments,key=lambda s:s['start_ms']):
        if segment['start_ms']>cursor:
            gaps.append({'start_ms':cursor,'end_ms':segment['start_ms'],'reason':'CALIBRATION_CHANGED_OR_NO_FIT'})
        cursor=max(cursor,segment['end_ms'])
    if cursor<duration:
        gaps.append({'start_ms':cursor,'end_ms':duration,'reason':'CALIBRATION_CHANGED_OR_NO_FIT'})
    return {**evidence,'ball_3d':{**ball,'segments':segments,'gaps':gaps,'calibration_stale':True,
        'limitations':[*ball.get('limitations',[]),'球台标定已修改；旧标定对应的三维球路已隐藏，需要重新计算。']}}


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
    # Local calibrated candidates have usable positions but no inferred phase.
    # Keep the existing phase-specific denominators and expose a separate total.
    def aggregate(rows):
        grid = [[0]*3 for _ in range(3)]
        for c in rows:
            grid[('SHORT','HALF_LONG','LONG').index(c['depth'])][('LEFT','CENTER','RIGHT').index(c['lateral'])] += 1
        return {'sample_count':len(rows), 'grid':grid}
    all_estimates = [c for c in complete if c['status'] != 'REJECTED']
    confirmed = [c for c in complete if c['status'] == 'CONFIRMED'
                 and c.get('phase') in ('SERVE','RECEIVE','RALLY')]
    unclassified = sum(c.get('phase', 'UNKNOWN') == 'UNKNOWN' for c in all_estimates)
    result['all_placements'] = aggregate(confirmed)
    result['unclassified_count'] = unclassified
    result['model_estimates']['all_placements'] = aggregate(all_estimates)
    result['model_estimates']['unclassified_count'] = unclassified
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
    result.setdefault('pose', None)
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


class CalibrationWindow(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1,max_length=128)
    corners: list[tuple[float,float]] = Field(min_length=4,max_length=4)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    base_version: int = Field(ge=0)
    timeline_version: int = Field(ge=1)
    analysis_artifact_id: str | None = None
    calibrations: list[CalibrationWindow] | None = Field(default=None,max_length=100)
    corners: list[tuple[float,float]] | None = Field(default=None,min_length=4,max_length=4)
    calibration_start_ms: int = Field(default=0,ge=0)
    calibration_end_ms: int | None = Field(default=None,gt=0)
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
    current_review = review_data(db,video,body.timeline_version)
    if body.analysis_artifact_id != current_review['analysis_artifact_id']:
        raise ApiError(409,'REPLAY_EVIDENCE_CONFLICT','分析证据已更新，请刷新后重新复核。')
    old_windows = current_review['calibrations']
    if body.calibrations is not None:
        windows = [c.model_dump(mode='json') for c in body.calibrations]
    elif {'corners','calibration_start_ms','calibration_end_ms'} & body.model_fields_set:
        # A legacy client edits only the mirrored first window. Other cameras
        # remain intact even when the client cannot represent the new list.
        first = old_windows[0] if old_windows else None
        corners = body.corners if 'corners' in body.model_fields_set else first['corners'] if first else None
        corners = [list(point) for point in corners] if corners is not None else None
        start = body.calibration_start_ms if 'calibration_start_ms' in body.model_fields_set else first['start_ms'] if first else 0
        end = body.calibration_end_ms if body.calibration_end_ms is not None else first['end_ms'] if first else video.duration_ms
        if not 0 <= start < end <= video.duration_ms:
            raise ApiError(422,'INVALID_CALIBRATION_RANGE','标定区间超出原视频。')
        windows = ([{'id':first['id'] if first else 'legacy','corners':corners,
                    'start_ms':start,'end_ms':end}] if corners is not None else []) + old_windows[1:]
    else:
        windows = old_windows
    windows = sorted(windows,key=lambda c:(c['start_ms'],c['id']))
    if len({c['id'] for c in windows}) != len(windows):
        raise ApiError(422,'DUPLICATE_CALIBRATION_ID','标定区间的标识不能重复。')
    for index,calibration in enumerate(windows):
        if not 0 <= calibration['start_ms'] < calibration['end_ms'] <= video.duration_ms:
            raise ApiError(422,'INVALID_CALIBRATION_RANGE','标定区间超出原视频。')
        if index and windows[index-1]['end_ms'] > calibration['start_ms']:
            raise ApiError(422,'CALIBRATION_OVERLAP','不同机位的标定区间不能重叠。')
        try:
            table_homography(calibration['corners'])
        except ValueError as exc:
            raise ApiError(422,'INVALID_CALIBRATION',str(exc))
    old_by_id,new_by_id = {c['id']:c for c in old_windows},{c['id']:c for c in windows}
    changed = {key for key in old_by_id.keys() | new_by_id.keys() if old_by_id.get(key) != new_by_id.get(key)}
    if any(key in new_by_id for key in changed) and body.fixed_camera is not True:
        raise ApiError(422,'INVALID_CALIBRATION','新增或修改标定时必须确认区间内机位固定。')
    affected = [c for key in changed for c in (old_by_id.get(key),new_by_id.get(key)) if c]
    is_affected = lambda r:any(c['start_ms'] <= r['time_ms'] < c['end_ms'] for c in affected)
    requested = [r.model_dump(mode='json') for r in body.reviews]
    if len({(r['rally_id'],r['time_ms']) for r in requested}) != len(requested):
        raise ApiError(422,'INVALID_LANDING_REVIEW','同一落点不能重复提交复核。')
    if previous and any(is_affected(r) for r in requested):
        raise ApiError(422,'CALIBRATION_CHANGED','请清除变更区间内的落点复核；其他机位的复核会保留。')
    # Preserve unrelated confirmations even if a calibration editor sends no
    # reviews. An analysis/timeline change must never revive stale reviews.
    preserved = {(r['rally_id'],r['time_ms']):r for r in current_review['reviews'] if not is_affected(r)}
    for review in requested:
        preserved[(review['rally_id'],review['time_ms'])] = review
    reviews = sorted(preserved.values(),key=lambda r:(r['rally_id'],r['time_ms']))
    if len(reviews) > 2000:
        raise ApiError(422,'TOO_MANY_LANDING_REVIEWS','单个视频最多保存 2000 条落点复核。')
    data = {'analysis_artifact_id':body.analysis_artifact_id,'calibrations':windows,
            **calibration_legacy_fields(windows,video.duration_ms),'reviews':reviews}
    available={};cache={}
    draft={**data,'reviews':[]}
    wanted={r['rally_id'] for r in reviews}
    for rally in rows:
        if rally.type=='RALLY' and rally.id in wanted:
            for c in rally_evidence(db,s3,video,rally,rows,draft,cache)[2]:
                available[(rally.id,c['time_ms'])]=c
    seen=set()
    for review in reviews:
        key=(review['rally_id'],review['time_ms'])
        if (key not in available or key in seen or (review['status']=='CONFIRMED' and
                (review['phase']=='UNKNOWN' or available[key].get('table_xy') is None
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
