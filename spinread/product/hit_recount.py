"""Publish improved hit estimates without replacing reviewed segment boundaries."""
from sqlalchemy import select
from spinread.core.models import Video, Timeline, TimelineItem, TimelineActivePointer, PipelineRun, utcnow
from spinread.product.timeline import VersionConflict, EditError, CORRECTION_STAGES
from spinread.pipeline import orchestrator
from spinread.pipeline.dag import PIPELINE_VERSION
from pingpong_training.analysis.audio_onset import RACKET_DETECTOR_VERSION


def recount_hits(db, video_id, base_version, times_ms):
    video = db.scalar(select(Video).where(Video.id == video_id).with_for_update())
    if video is None or video.deleted_at:
        raise EditError('VIDEO_UNAVAILABLE', 'Video unavailable')
    if db.scalar(select(PipelineRun.id).where(PipelineRun.video_id == video_id, PipelineRun.state == 'RUNNING')):
        raise EditError('RUN_ACTIVE', 'Wait for the current analysis')
    pointer = db.get(TimelineActivePointer, video_id)
    if pointer is None:
        raise EditError('NO_TIMELINE', 'No timeline')
    previous = db.get(Timeline, pointer.timeline_id)
    if previous.version != base_version:
        raise VersionConflict(previous)
    old = db.scalars(select(TimelineItem).where(TimelineItem.timeline_id == previous.id,
        TimelineItem.status == 'ACTIVE').order_by(TimelineItem.start_ms)).all()
    timeline = Timeline(video_id=video_id, version=previous.version+1, state='PUBLISHED', created_by='MODEL')
    db.add(timeline); db.flush()
    mapping = {}
    times = sorted(set(int(t) for t in times_ms if 0 <= t < (video.duration_ms or 0)))
    for row in old:
        if row.type == 'HIT_CANDIDATE': continue
        copy = TimelineItem(timeline_id=timeline.id, type=row.type, start_ms=row.start_ms,
            end_ms=row.end_ms, actor=row.actor, attributes=dict(row.attributes), confidence=row.confidence,
            provenance=dict(row.provenance), status=row.status)
        if row.type == 'RALLY':
            copy.attributes.update(hits=sum(row.start_ms<=t<row.end_ms for t in times),
                hit_detector=RACKET_DETECTOR_VERSION, hit_count_estimated=True)
        db.add(copy); db.flush(); mapping[row.id] = copy
    for row in old:
        copy = mapping.get(row.id)
        if copy is None: continue
        copy.parent_id = mapping[row.parent_id].id if row.parent_id in mapping else None
        if row.type == 'RALLY':
            for t in times:
                if row.start_ms <= t < row.end_ms:
                    db.add(TimelineItem(timeline_id=timeline.id,parent_id=copy.id,type='HIT_CANDIDATE',
                        start_ms=t,end_ms=min(t+40,row.end_ms),attributes={'detector':RACKET_DETECTOR_VERSION},
                        confidence=None, provenance={'source':'MODEL','source_id':RACKET_DETECTOR_VERSION},status='ACTIVE'))
    pointer.timeline_id, pointer.switched_at = timeline.id, utcnow()
    run = PipelineRun(video_id=video_id,pipeline_version=PIPELINE_VERSION,trigger='CORRECTION')
    db.add(run);db.flush()
    orchestrator.tick(db,run.id,only_stages=CORRECTION_STAGES)
    return timeline, run
