"""Replace model rally children, preserving reviewed top-level activity intervals."""
from sqlalchemy import select
from spinread.core.models import Video, Timeline, TimelineItem, TimelineActivePointer, PipelineRun, utcnow
from spinread.product.timeline import EditError, VersionConflict, CORRECTION_STAGES
from spinread.pipeline.dag import PIPELINE_VERSION
from spinread.pipeline import orchestrator
from pingpong_training.analysis.rally import STAGE_VERSION
from pingpong_training.analysis.audio_onset import RACKET_DETECTOR_VERSION


def resegment_rallies(db, video_id, base_version, rallies):
    video=db.scalar(select(Video).where(Video.id==video_id).with_for_update())
    if video is None or video.deleted_at: raise EditError('VIDEO_UNAVAILABLE','Video unavailable')
    if db.scalar(select(PipelineRun.id).where(PipelineRun.video_id==video_id,PipelineRun.state=='RUNNING')):
        raise EditError('RUN_ACTIVE','Analysis in progress')
    pointer=db.get(TimelineActivePointer,video_id)
    if pointer is None: raise EditError('NO_TIMELINE','No timeline')
    previous=db.get(Timeline,pointer.timeline_id)
    if previous.version!=base_version: raise VersionConflict(previous)
    rows=db.scalars(select(TimelineItem).where(TimelineItem.timeline_id==previous.id,TimelineItem.status=='ACTIVE').order_by(TimelineItem.start_ms)).all()
    timeline=Timeline(video_id=video_id,version=previous.version+1,state='PUBLISHED',created_by='MODEL')
    db.add(timeline);db.flush()
    # Keep non-rally branches and their provenance. Only rally subtrees change.
    removed={r.id for r in rows if r.type in ('RALLY','HIT_CANDIDATE')}
    while True:
        expanded=removed|{r.id for r in rows if r.parent_id in removed}
        if expanded==removed:break
        removed=expanded
    mapping={}
    for row in rows:
        if row.id in removed:continue
        copy=TimelineItem(timeline_id=timeline.id,type=row.type,start_ms=row.start_ms,end_ms=row.end_ms,
            actor=row.actor,attributes=dict(row.attributes),confidence=row.confidence,provenance=dict(row.provenance),status=row.status)
        db.add(copy);db.flush();mapping[row.id]=copy
    for row in rows:
        if row.id in mapping:
            mapping[row.id].parent_id=mapping[row.parent_id].id if row.parent_id in mapping else None
    parents=[mapping[r.id] for r in rows if r.id in mapping and r.parent_id is None and r.type=='RALLY_LIKE']
    for r in rallies:
        parent=next((p for p in parents if p.start_ms<=r.start_ms<r.end_ms<=p.end_ms),None)
        if parent is None:raise EditError('INVALID_RALLY','Rally outside activity interval')
        row=TimelineItem(timeline_id=timeline.id,parent_id=parent.id,type='RALLY',start_ms=r.start_ms,end_ms=r.end_ms,
            attributes={'hits':len(r.hits_ms),'poc':STAGE_VERSION,'hit_detector':RACKET_DETECTOR_VERSION,'hit_count_estimated':True},
            confidence=None,provenance={'source':'MODEL','source_id':STAGE_VERSION},status='ACTIVE')
        db.add(row);db.flush()
        for t in r.hits_ms:
            db.add(TimelineItem(timeline_id=timeline.id,parent_id=row.id,type='HIT_CANDIDATE',start_ms=t,end_ms=min(t+40,r.end_ms),
                attributes={'detector':RACKET_DETECTOR_VERSION},confidence=None,
                provenance={'source':'MODEL','source_id':RACKET_DETECTOR_VERSION},status='ACTIVE'))
    pointer.timeline_id,pointer.switched_at=timeline.id,utcnow()
    run=PipelineRun(video_id=video_id,pipeline_version=PIPELINE_VERSION,trigger='CORRECTION')
    db.add(run);db.flush();orchestrator.tick(db,run.id,only_stages=CORRECTION_STAGES)
    return timeline,run
