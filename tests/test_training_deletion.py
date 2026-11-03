from sqlalchemy import select
from test_quizzes import quiz_client
from test_rally_merge import node, tree
from spinread.product.timeline import _apply_ops, _validate
from spinread.core.models import TimelineItem, Timeline, TrainingAnnotationRevision


def test_last_rally_removes_wrapper_without_resurrecting_training():
    roots = [node('a', 'RALLY_LIKE', 1000, 5000, [node('r', 'RALLY', 1000, 5000, [node('hit', 'HIT_CANDIDATE', 2500, 2501)])]), node('b', 'BREAK', 5000, 9000)]
    _apply_ops(roots, [{'op':'DELETE','timeline_item_id':'r'}])
    assert [r.id for r in roots] == ['b']


def test_other_rallies_survive_and_empty_wrappers_are_cleaned_on_last_delete():
    roots = tree()
    _apply_ops(roots, [{'op':'DELETE','timeline_item_id':'r1'}])
    assert roots[0].type == 'RALLY_LIKE'
    assert [c.id for c in roots[0].children] == ['r0']
    _apply_ops(roots, [{'op':'DELETE','timeline_item_id':'r0'}])
    assert [r.id for r in roots] == ['gap','b']
    assert [c.id for c in roots[-1].children] == ['r2','r3']
    _validate(roots, 20000)


def test_non_rally_children_preserved_as_non_training():
    roots = [node('a','RALLY_LIKE',0,10000,[node('r','RALLY',1000,2000),node('event','OTHER_EVENT',5000,6000)])]
    _apply_ops(roots,[{'op':'DELETE','timeline_item_id':'r'}])
    assert roots[0].type == 'UNKNOWN'
    assert roots[0].children[0].id == 'event'


def test_delete_chapter_api_preserves_history_annotations_and_other_rallies(quiz_client):
    c, factory = quiz_client
    with factory.begin() as db:
        db.add(TrainingAnnotationRevision(video_id='video',version=1,segments=[],author_id='owner'))
        for root in tree():
            def emit(n,parent=None):
                db.add(TimelineItem(id=n.id,timeline_id='timeline',parent_id=parent,type=n.type,start_ms=n.start_ms,end_ms=n.end_ms,attributes={},provenance={},status='ACTIVE'))
                db.flush()
                for child in n.children: emit(child,n.id)
            emit(root)
    body={'base_timeline_version':1,'operations':[{'op':'DELETE','timeline_item_id':'r0'},{'op':'DELETE','timeline_item_id':'r1'}]}
    path='/api/videos/video/timeline-edits'
    assert c.post(path,json=body,headers={'X-Test-User':'other'}).status_code == 403
    response=c.post(path,json=body)
    assert response.status_code == 200, response.text
    assert c.post(path,json=body).status_code == 409
    active=c.get('/api/videos/video/timelines/active').json()
    assert active['version'] == 2
    assert len([i for i in active['items'] if i['type']=='RALLY']) == 2
    assert not any(i['start_ms'] < 4000 for i in active['items'])
    assert c.get('/api/videos/video/training-annotations').json()['version'] == 1
    old=c.get('/api/videos/video/timelines/1').json()
    assert len([i for i in old['items'] if i['type']=='RALLY']) == 4
