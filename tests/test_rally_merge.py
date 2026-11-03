from copy import deepcopy
import pytest
from spinread.product.timeline import _Node, _apply_ops, _validate, EditError


def node(id, kind, start, end, children=(), parent=None):
    n = _Node(id, parent, kind, start, end, None, {}, None, {}, list(children))
    for c in n.children:
        c.parent_id = id
    return n


def tree():
    return [node('a', 'RALLY_LIKE', 0, 4000, [
        node('r0', 'RALLY', 100, 800), node('r1', 'RALLY', 1000, 2000, [node('h1', 'HIT_CANDIDATE', 1500, 1501)])]),
        node('gap', 'BREAK', 4000, 11000),
        node('b', 'RALLY_LIKE', 11000, 20000, [
            node('r2', 'RALLY', 12000, 13000, [node('h2', 'HIT_CANDIDATE', 12500, 12501)]),
            node('r3', 'RALLY', 16000, 19000)])]


def test_merge_keeps_preparation_and_preserves_outside_rallies_and_hits():
    roots = tree()
    _apply_ops(roots, [{'op': 'MERGE_RALLIES', 'timeline_item_ids': ['r2', 'r1']}])
    assert [(r.start_ms, r.end_ms) for r in roots] == [(0, 1000), (1000, 13000), (13000, 20000)]
    assert roots[0].children[0].id == 'r0'
    assert roots[-1].children[0].id == 'r3'
    rally = roots[1].children[0]
    assert (rally.start_ms, rally.end_ms) == (1000, 13000)
    assert [h.id for h in rally.children] == ['h1', 'h2']
    assert rally.attributes['manual_merged']
    assert _validate(roots, 20000) == 8


@pytest.mark.parametrize('ids', [['r1', 'r3'], ['r1'], ['r1', 'r1'], ['r1', 'missing'], ['r1', 'gap']])
def test_invalid_selection_is_rejected_without_mutation(ids):
    roots = tree(); before = deepcopy(roots)
    with pytest.raises(EditError):
        _apply_ops(roots, [{'op': 'MERGE_RALLIES', 'timeline_item_ids': ids}])
    assert roots == before


def test_merge_three_siblings_in_one_activity():
    roots = [node('a', 'RALLY_LIKE', 0, 10000, [node('r1', 'RALLY', 1000, 2000), node('r2', 'RALLY', 3000, 4000), node('r3', 'RALLY', 6000, 7000)])]
    _apply_ops(roots, [{'op': 'MERGE_RALLIES', 'timeline_item_ids': ['r1', 'r2', 'r3']}])
    rallies = [c for r in roots for c in r.children if c.type == 'RALLY']
    assert len(rallies) == 1
    assert (rallies[0].start_ms, rallies[0].end_ms) == (1000, 7000)
    _validate(roots, 10000)

from test_quizzes import quiz_client
from sqlalchemy import select
from spinread.core.models import TimelineItem, Timeline, TimelineActivePointer


def test_api_merge_versions_and_quiz_workflow(quiz_client):
    c, factory = quiz_client
    with factory.begin() as db:
        for root in tree():
            def emit(n, parent=None):
                db.add(TimelineItem(id=n.id, timeline_id='timeline', parent_id=parent,
                    type=n.type, start_ms=n.start_ms, end_ms=n.end_ms, attributes=n.attributes,
                    provenance={}, status='ACTIVE'))
                db.flush()
                for child in n.children:
                    emit(child, n.id)
            emit(root)
    path = '/api/videos/video/timeline-edits'
    body = {'base_timeline_version':1, 'operations':[{'op':'MERGE_RALLIES','timeline_item_ids':['r1','r2']}]}
    assert c.post(path, json=body, headers={'X-Test-User':'other'}).status_code == 403
    assert c.post(path, json=body | {'operations':[{'op':'MERGE_RALLIES','timeline_item_ids':['r1','r3']}]}).status_code == 422
    old_quiz = c.post('/api/videos/video/quiz-items', json={'timeline_version':1,'start_ms':1000,'contact_ms':1300,'pause_ms':1700,'end_ms':2000,'approval':'APPROVED'})
    assert old_quiz.status_code == 201
    response = c.post(path, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['version'] == 2
    assert c.post(path, json=body).status_code == 409
    active = c.get('/api/videos/video/timelines/active').json()
    merged = next(i for i in active['items'] if i['type'] == 'RALLY' and i['start_ms'] == 1000)
    assert merged['end_ms'] == 13000 and merged['attributes']['hits'] == 2
    assert c.get('/api/videos/video/quiz-items').json()['items'][0]['stale']
    assert not c.get('/api/quiz-sets/recommended').json()['items']
    quiz = c.post('/api/videos/video/quiz-items', json={'timeline_version':2,'start_ms':1000,'contact_ms':12000,'pause_ms':12500,'end_ms':13000,'approval':'APPROVED'})
    assert quiz.status_code == 201, quiz.text
    assert c.get('/api/quiz-sets/recommended').json()['items'][0]['id'] == quiz.json()['id']
    with factory() as db:
        assert len(db.scalars(select(TimelineItem).where(TimelineItem.timeline_id=='timeline',TimelineItem.type=='RALLY')).all()) == 4
        assert len(db.scalars(select(Timeline)).all()) == 2
