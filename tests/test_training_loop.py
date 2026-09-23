"""Isolated contracts for player/coach permissions, plans, and retest history."""
import uuid
from datetime import timedelta
import pytest
from sqlalchemy import select
from test_quizzes import quiz_client
from spinread.core.models import (User, Video, Timeline, TimelineActivePointer, AnalysisReport,
    Finding, MetricValue, PipelineRun, Artifact, PlanItem, PlanItemRetest, AuditEvent, utcnow)


CONTEXT = dict(practice_context="short receive", target_identity="player-A", camera_setup="tripod-A", opponent_or_feeder="coach-A")


@pytest.fixture
def loop(quiz_client):
    c, factory = quiz_client
    with factory.begin() as db:
        db.add(User(id="coach", email="coach@test.local", display_name="Coach", password_hash="unused", role="COACH"))
        db.add(Video(id="retest", owner_id="owner", filename="retest.mp4", state="READY", duration_ms=20000, training_context=CONTEXT))
        db.get(Video, "video").training_context = CONTEXT
        db.add(Timeline(id="retest-tl", video_id="retest", version=1, state="PUBLISHED"))
        db.flush()
        db.add(TimelineActivePointer(video_id="retest", timeline_id="retest-tl"))
        for vid, value in [("video", 4000), ("retest", 8000)]:
            db.add(PipelineRun(id=f"run-{vid}", video_id=vid, pipeline_version="test", trigger="UPLOAD", state="SUCCEEDED"))
            db.flush()
            db.add(Artifact(video_id=vid, pipeline_run_id=f"run-{vid}", stage="QUALITY", stage_version="quality-heuristic-0.1.0",
                object_key=f"{vid}.json", content_hash=vid, metrics={"audio_present": True}, created_at=utcnow()-timedelta(seconds=1)))
            db.add(AnalysisReport(id=f"report-{vid}", video_id=vid, timeline_version=1, state="PUBLISHED", published_at=utcnow(),
                metric_versions={"valid_duration_ms": "v1"}, structured={"metrics": {"valid_duration_ms": value}}))
            db.add(MetricValue(video_id=vid, pipeline_run_id=f"run-{vid}", timeline_version=1, metric_name="valid_duration_ms",
                metric_version="v1", value={"result": value}, sample_count=12))
        db.flush()
        db.add(Finding(id="finding", report_id="report-video", category="ACTIVITY_MIX", observation="Review breaks",
            sample_count=12, state="PUBLISHED", priority_score=.7))
    return c, factory


def post(c, path, body, actor="owner", key=None, method="POST"):
    return c.request(method, "/api"+path, json=body,
        headers={"X-Test-User": actor, "Idempotency-Key": key or str(uuid.uuid4())})


def share(c, video="video"):
    g = post(c, "/coach-grants", {"coach_email": "coach@test.local"})
    assert g.status_code == 200, g.text
    r = post(c, f"/videos/{video}/consents", {"purpose": "COACH_VIEW", "granted_to": "coach"})
    assert r.status_code == 200, r.text
    return g.json(), r.json()


def plan(c):
    r = post(c, "/training-plans/generate", {"report_id": "report-video"})
    assert r.status_code == 200, r.text
    return r.json()["plan"]["items"][0]


def test_coach_needs_both_grants_and_revocation_denies_every_read(loop):
    c, factory = loop
    h = {"X-Test-User": "coach"}
    assert c.get('/api/videos/video', headers=h).status_code == 403
    post(c, '/coach-grants', {'coach_email': 'coach@test.local'})
    assert c.get('/api/videos/video', headers=h).status_code == 403
    _, consent = share(c)
    paths=['/videos/video','/videos/video/timelines/active','/videos/video/timelines/1',
        '/videos/video/reports/active','/reports/report-video/evidence','/videos/video/context']
    for path in paths: assert c.get('/api'+path, headers=h).status_code == 200, path
    assert c.delete('/api/videos/video', headers=h).status_code == 403
    assert post(c, '/videos/video/timeline-edits', {'base_timeline_version':1,'operations':[]}, actor='coach').status_code == 403
    assert post(c, '/consents/'+consent['id']+'/revoke', {'base_version':1}).status_code == 200
    for path in paths+['/videos/video/stream/master.m3u8','/videos/video/media/proxy','/videos/video/thumbs/0.jpg']:
        assert c.get('/api'+path, headers=h).status_code == 403, path
    with factory() as db:
        assert db.scalar(select(AuditEvent).where(AuditEvent.kind=='CONSENT_REVOKED')) is not None


def test_feedback_is_versioned_scoped_and_idempotent(loop):
    c, _ = loop
    grant, consent = share(c)
    req=post(c,'/review-requests',{'video_id':'video','coach_id':'coach','question':'How should I practice?'}).json()
    path='/review-requests/'+req['id']+'/feedback'
    body={'base_version':1,'body':'Review this interval','start_ms':1000,'end_ms':3000}
    assert post(c,path,body,actor='other').status_code == 403
    assert post(c,path,body|{'end_ms':21000},actor='coach').status_code == 422
    r=post(c,path,body,actor='coach',key='feedback-retry-key')
    assert r.status_code == 200, r.text
    assert r.json()['status']=='CLAIMED' and len(r.json()['feedback'])==1
    assert post(c,path,body,actor='coach',key='feedback-retry-key').json()==r.json()
    assert post(c,path,body,actor='coach').status_code==409
    done=post(c,path,body|{'base_version':2,'complete':True},actor='coach')
    assert done.json()['status']=='DONE'
    post(c,'/coach-grants/'+grant['id']+'/revoke',{'base_version':1})
    assert c.get('/api/review-requests/'+req['id'],headers={'X-Test-User':'coach'}).status_code==403
    assert len(c.get('/api/review-requests/'+req['id']).json()['feedback'])==2
    # Cached responses must not bypass revoked authorization.
    assert post(c,path,body,actor='coach',key='feedback-retry-key').status_code==403


def test_coach_locked_plans_survive_regeneration_and_keep_history(loop):
    c, factory = loop
    item=plan(c)
    assert post(c,'/training-plans/generate',{'report_id':'report-video'}).json()['added']==0
    path=f"/training-plans/{item['plan_id']}/items/{item['id']}"
    assert post(c,path,{'base_version':1,'title':'Coach edit'},actor='coach',method='PATCH').status_code==403
    share(c)
    r=post(c,path,{'base_version':1,'title':'Coach prescription','locked_by_coach':True},actor='coach',method='PATCH')
    assert r.status_code==200, r.text
    assert r.json()['source']=='COACH' and r.json()['version']==2
    assert post(c,path,{'base_version':2,'title':'overwrite'},method='PATCH').status_code==403
    assert post(c,path,{'base_version':2,'player_note':'Completed two sessions','status':'DONE'},method='PATCH').status_code==200
    updated=post(c,'/training-plans/generate',{'report_id':'report-video'}).json()['plan']['items'][0]
    assert updated['title']=='Coach prescription' and updated['locked_by_coach'] and updated['status']=='DONE'
    assert len(updated['history'])==2


def test_retest_gates_and_immutable_result(loop):
    c, factory = loop
    item=plan(c)
    path=f"/training-plans/{item['plan_id']}/items/{item['id']}/retests"
    r=post(c,path,{'base_version':1,'video_id':'retest'},key='retest-idempotency')
    assert r.status_code==200, r.text
    result=r.json()['result']
    assert result['comparable'] and result['relative_change']==1 and result['success']
    assert post(c,path,{'base_version':1,'video_id':'retest'},key='retest-idempotency').json()['id']==r.json()['id']
    with factory.begin() as db:
        db.get(Video,'retest').training_context=CONTEXT|{'camera_setup':'different'}
        db.get(Video,'retest').context_version+=1
    newer=post(c,path,{'base_version':1,'video_id':'retest'}).json()['result']
    assert not newer['comparable'] and 'CAMERA_SETUP_MISMATCH' in newer['reasons']
    assert newer['delta'] is None and newer['success'] is None
    with factory() as db:
        assert db.get(PlanItemRetest,r.json()['id']).result==result
    assert post(c,path,{'base_version':1,'video_id':'video'}).json()['result']['comparable'] is False


@pytest.mark.parametrize('change,reason', [('samples','INSUFFICIENT_SAMPLES'),('version','METRIC_VERSION_MISMATCH'),('missing','MISSING_TARGET_IDENTITY'),('stale','RETEST_REPORT_STALE')])
def test_incompatible_retest_never_emits_progress(loop,change,reason):
    c,factory=loop
    item=plan(c)
    with factory.begin() as db:
        if change=='samples': db.scalar(select(MetricValue).where(MetricValue.video_id=='retest')).sample_count=1
        if change=='version': db.get(AnalysisReport,'report-retest').metric_versions={'valid_duration_ms':'v2'}
        if change=='missing': db.get(Video,'retest').training_context={}
        if change=='stale': db.get(Timeline,'retest-tl').version=2
    r=post(c,f"/training-plans/{item['plan_id']}/items/{item['id']}/retests",{'base_version':1,'video_id':'retest'})
    assert r.status_code==200,r.text
    result=r.json()['result']
    assert reason in result['reasons'] and result['delta'] is None


def test_context_conflicts_registration_and_key_reuse(loop):
    c,_=loop
    body=CONTEXT|{'base_version':1}
    path='/videos/video/context'
    assert post(c,path,body,method='PATCH').status_code==200
    assert post(c,path,body,method='PATCH').status_code==409
    assert post(c,'/training-plans/generate',{'report_id':'report-video'},key='same-key-across-routes').status_code==200
    assert post(c,'/coach-grants',{'coach_email':'coach@test.local'},key='same-key-across-routes').status_code==409
    r=c.post('/api/auth/register',json={'email':'new@test.local','password':'test-password','display_name':'New coach','role':'COACH'})
    assert r.status_code==201,r.text
    assert r.json()['user']['role']=='COACH'
    assert c.post('/api/auth/register',json={'email':'admin@test.local','password':'test-password','display_name':'Bad','role':'ADMIN'}).status_code==422


def test_coach_created_task_belongs_to_player_and_replay_checks_permission(loop):
    c, _ = loop
    _, consent = share(c)
    body = {'report_id': 'report-video', 'title': 'Coach task', 'priority': 4,
            'drill': {'description': 'Practice with coach'},
            'retest': {'context': CONTEXT['practice_context'], 'metric': 'active_fraction'}}
    r = post(c, '/training-plans/items', body, actor='coach', key='coach-create-task')
    assert r.status_code == 200, r.text
    assert r.json()['source'] == 'COACH' and r.json()['locked_by_coach']
    result = c.get('/api/users/owner/training-plans/active').json()['plan']
    assert result['user_id'] == 'owner' and result['items'][0]['priority'] == 4
    post(c, '/consents/'+consent['id']+'/revoke', {'base_version': 1})
    assert post(c, '/training-plans/items', body, actor='coach', key='coach-create-task').status_code == 403


def test_retest_results_require_permission_for_both_videos(loop):
    c, _ = loop
    item = plan(c)
    path = f"/training-plans/{item['plan_id']}/items/{item['id']}/retests"
    assert post(c, path, {'base_version': 1, 'video_id': 'retest'}).status_code == 200
    share(c)
    h = {'X-Test-User': 'coach'}
    assert c.get('/api'+path, headers=h).json()['items'] == []
    _, consent = share(c, 'retest')
    assert len(c.get('/api'+path, headers=h).json()['items']) == 1
    post(c, '/consents/'+consent['id']+'/revoke', {'base_version': 1})
    assert c.get('/api'+path, headers=h).json()['items'] == []
