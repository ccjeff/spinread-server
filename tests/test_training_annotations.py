from sqlalchemy import select
from test_quizzes import quiz_client
from spinread.core.models import TrainingAnnotationRevision, Timeline, TimelineActivePointer, Video

PATH = '/api/videos/video/training-annotations'
SEGMENT = {'id':'serve', 'start_ms':1000, 'end_ms':18000, 'title':'发接发训练', 'feeding':'SERVE_RECEIVE',
    'movement':'UNSPECIFIED', 'target':'FAR', 'actions':[{'hand':'BACKHAND','stroke':'拉球','incoming_spin':'TOPSPIN','movement':'转正手位'}]}

def test_save_read_revision_and_rerun_independence(quiz_client):
    c, factory = quiz_client
    assert c.get(PATH).json()['version'] == 0
    saved = c.put(PATH, json={'base_version':0,'segments':[SEGMENT]})
    assert saved.status_code == 200, saved.text
    assert saved.json()['version'] == 1
    assert c.get(PATH).json()['segments'][0]['actions'] == SEGMENT['actions']
    with factory.begin() as db:
        db.add(Timeline(id='new-timeline',video_id='video',version=2,state='PUBLISHED'))
        db.flush()
        db.get(TimelineActivePointer,'video').timeline_id = 'new-timeline'
    assert c.get(PATH).json()['segments'][0]['id'] == 'serve'
    assert c.put(PATH,json={'base_version':0,'segments':[]}).status_code == 409
    assert c.put(PATH,json={'base_version':1,'segments':[]}).status_code == 200
    with factory() as db:
        history = db.scalars(select(TrainingAnnotationRevision).order_by(TrainingAnnotationRevision.version)).all()
        assert len(history) == 2 and history[0].segments[0]['title'] == '发接发训练'
        assert history[1].segments == []

def test_validation_access_and_deleted_video(quiz_client):
    c, factory = quiz_client
    payload = {'base_version':0,'segments':[SEGMENT]}
    assert c.get(PATH,headers={'X-Test-User':'other'}).status_code == 403
    assert c.put(PATH,json=payload,headers={'X-Test-User':'other'}).status_code == 403
    for bad in [SEGMENT | {'start_ms':-1}, SEGMENT | {'end_ms':21000}, SEGMENT | {'end_ms':1000},
                SEGMENT | {'title':'  '}, SEGMENT | {'feeding':'FAKE'}, SEGMENT | {'start_ms':1.5}]:
        assert c.put(PATH,json={'base_version':0,'segments':[bad]}).status_code == 422
    assert c.put(PATH,json={'base_version':0,'segments':[SEGMENT, SEGMENT | {'id':'other'}]}).status_code == 422
    assert c.put(PATH,json={'base_version':0,'segments':[SEGMENT, SEGMENT | {'start_ms':18000,'end_ms':19000}]}).status_code == 422
    assert c.get(PATH).json()['version'] == 0
    with factory.begin() as db:
        from spinread.core.models import utcnow
        db.get(Video,'video').deleted_at = utcnow()
    assert c.get(PATH).status_code == 404
    assert c.put(PATH,json=payload).status_code == 404

def test_exact_partition_and_short_remainder(quiz_client):
    c, _ = quiz_client
    parts=[SEGMENT | {'id':'left','end_ms':1100}, SEGMENT | {'id':'right','start_ms':1100}]
    response=c.put(PATH,json={'base_version':0,'segments':list(reversed(parts))})
    assert response.status_code == 200, response.text
    assert [s['id'] for s in response.json()['segments']] == ['left','right']
