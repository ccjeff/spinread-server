from test_quizzes import quiz_client
from spinread.core.models import TimelineItem, Video, TimelineActivePointer
from spinread.product.hit_recount import recount_hits
from spinread.product.timeline import apply_timeline_edits
from sqlalchemy import select


def test_recount_preserves_boundaries_history_and_updates_counts(quiz_client):
    _, factory=quiz_client
    with factory.begin() as db:
        db.add(TimelineItem(id='seg',timeline_id='timeline',type='RALLY_LIKE',start_ms=0,end_ms=10000,attributes={}))
        db.flush()
        db.add(TimelineItem(id='r',timeline_id='timeline',parent_id='seg',type='RALLY',start_ms=1000,end_ms=8000,attributes={'hits':99},provenance={'source':'USER'}))
    with factory.begin() as db:
        tl,run=recount_hits(db,'video',1,[1500,2000,2000,5000,9000])
        rows=db.scalars(select(TimelineItem).where(TimelineItem.timeline_id==tl.id)).all()
        rally=next(r for r in rows if r.type=='RALLY')
        assert (rally.start_ms,rally.end_ms,rally.attributes['hits'])==(1000,8000,3)
        assert rally.provenance=={'source':'USER'}
        assert len([r for r in rows if r.type=='HIT_CANDIDATE'])==3
        assert db.get(TimelineItem,'r').attributes['hits']==99
        run.state='SUCCEEDED'
        tl2,_=apply_timeline_edits(db,db.get(Video,'video'),2,[{'op':'UPDATE_BOUNDARY','timeline_item_id':rally.id,'start_ms':3000,'end_ms':8000}])
        db.flush()
        newer=db.scalar(select(TimelineItem).where(TimelineItem.timeline_id==tl2.id,TimelineItem.type=='RALLY'))
        assert newer.attributes['hits']==1


def test_resegment_keeps_activity_and_old_rally_history(quiz_client):
    from spinread.product.rally_resegment import resegment_rallies
    from pingpong_training.analysis.rally import RallyCandidate
    _, factory=quiz_client
    with factory.begin() as db:
        db.add(TimelineItem(id='seg',timeline_id='timeline',type='RALLY_LIKE',start_ms=0,end_ms=10000,
            attributes={},provenance={'source':'USER'}));db.flush()
        db.add(TimelineItem(id='old-rally',timeline_id='timeline',parent_id='seg',type='RALLY',start_ms=1000,end_ms=9000,attributes={'hits':4}))
    with factory.begin() as db:
        tl,_=resegment_rallies(db,'video',1,[RallyCandidate(1000,2000,(1000,1500),.5),RallyCandidate(4000,5500,(4000,5000),.5)])
        db.flush()
        rows=db.scalars(select(TimelineItem).where(TimelineItem.timeline_id==tl.id)).all()
        seg=next(r for r in rows if r.type=='RALLY_LIKE')
        assert (seg.start_ms,seg.end_ms,seg.provenance)==(0,10000,{'source':'USER'})
        assert len([r for r in rows if r.type=='RALLY'])==2
        assert len([r for r in rows if r.type=='HIT_CANDIDATE'])==4
        assert db.get(TimelineItem,'old-rally').end_ms==9000
