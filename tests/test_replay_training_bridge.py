"""No live DB: replay → private quiz → coach answer → preserved training task."""
import uuid
import pytest
from sqlalchemy import select
from test_replay import replay_client, PATH
from test_quizzes import quiz_client, ANSWER
from test_training_loop import share, post
from spinread.core.models import (User, Artifact, TimelineItem, QuizItem, QuizAttempt, QuizReferenceAnswer,
    AnalysisReport, ReplayAnalysis, ReplayReview, Timeline, TimelineActivePointer, PlanItem, Video, utcnow)


@pytest.fixture
def bridge(replay_client):
    c,factory,evidence = replay_client
    with factory.begin() as db:
        db.get(TimelineItem,'hit0').start_ms=100
        db.get(TimelineItem,'hit0').end_ms=140
        db.add(User(id='coach',email='coach@test.local',display_name='Coach',role='COACH',password_hash='unused'))
        db.add(AnalysisReport(id='report',video_id='video',timeline_version=1,state='PUBLISHED',
            published_at=utcnow(),metric_versions={},structured={}))
    return c,factory,evidence


def drafted(c):
    r=post(c,'/videos/video/replay-quiz-candidates',{'timeline_version':1,'rally_ids':['rally']})
    assert r.status_code==201,r.text
    return r.json()['items'][0]


def approved(c):
    item=drafted(c)
    r=c.post(f"/api/quiz-items/{item['id']}/revisions",json={
        **{k:item[k] for k in ('start_ms','contact_ms','pause_ms','end_ms')},
        'timeline_version':1,'base_version':item['version'],'approval':'APPROVED'})
    assert r.status_code==201,r.text
    item=r.json()
    r=c.post(f"/api/quiz-items/{item['id']}/attempts",json={**ANSWER,'item_version':item['version']},
        headers={'Idempotency-Key':str(uuid.uuid4())})
    assert r.status_code==201,r.text
    return item


def reviewing(c):
    grant,consent=share(c)
    req=post(c,'/review-requests',{'video_id':'video','coach_id':'coach'}).json()
    return req,grant,consent


def reference_body(item,version=1):
    return {'base_version':version,'quiz_item_id':item['id'],'quiz_item_version':item['version'],
        'timeline_version':1,'answer':{'spin':'UNKNOWN','length':'SHORT','acceptable_receives':['PUSH','FLICK'],
            'note':'Both are reasonable; keep the contact in front.'},
        'evidence':{'rally_id':'rally','contact_ms':100,'replay_artifact_id':'evidence'},'diagnosis':'EXECUTION'}


def test_replay_drafts_do_not_rewrite_attempts_or_auto_approve(bridge):
    c,factory,_=bridge
    body={'timeline_version':1,'rally_ids':['rally']}
    first=post(c,'/videos/video/replay-quiz-candidates',body,key='replay-draft-key')
    assert first.status_code==201,first.text
    assert first.json()['items'][0]['approval']=='DRAFT'
    assert post(c,'/videos/video/replay-quiz-candidates',body,key='replay-draft-key').json()==first.json()
    assert post(c,'/videos/video/replay-quiz-candidates',body).json()['items'][0]['id']==first.json()['items'][0]['id']
    assert post(c,'/videos/video/replay-quiz-candidates',body,actor='other').status_code==403
    assert post(c,'/videos/video/replay-quiz-candidates',body|{'timeline_version':2}).status_code==409
    assert post(c,'/videos/video/replay-quiz-candidates',body|{'rally_ids':['missing']}).status_code==422
    item=approved(c)
    assert drafted(c)['id']==item['id']
    with factory() as db:
        assert len(db.scalars(select(QuizAttempt)).all())==1
        assert len(db.scalars(select(QuizItem)).all())==2


def test_future_evidence_is_gated_by_exact_attempt_and_range(bridge):
    c,factory,evidence=bridge
    item=drafted(c)
    path=f"/api/quiz-items/{item['id']}/evidence"
    assert c.get(path).status_code==403
    item=approved(c)
    path=f"/api/quiz-items/{item['id']}/evidence"
    evidence['pose']={'coordinate_space':'HIP_RELATIVE_METERS','frames':[
        {'time_ms':100,'people':[]},{'time_ms':5000,'people':[]}]}
    r=c.get(path)
    assert r.status_code==200,r.text
    assert r.json()['replay']['end_ms']==item['end_ms']
    assert [f['time_ms'] for f in r.json()['replay']['pose']['frames']]==[100]
    assert r.json()['replay']['next_start_ms'] is None
    assert c.get(path,headers={'X-Test-User':'other'}).status_code==403
    with factory.begin() as db:
        db.add(Timeline(id='tl-next',video_id='video',version=2,state='PUBLISHED'));db.flush()
        db.get(TimelineActivePointer,'video').timeline_id='tl-next'
    assert c.get(path).status_code==409
    assert len(c.get(f"/api/quiz-items/{item['id']}/attempts").json())==1


def test_reference_answers_are_immutable_scoped_and_revocable(bridge):
    c,factory,_=bridge
    item=approved(c);req,grant,_=reviewing(c)
    path=f"/review-requests/{req['id']}/quiz-reference-answers"
    body=reference_body(item)
    assert post(c,path,body,actor='owner').status_code==403
    invalid={**body,'evidence':{'rally_id':'rally','contact_ms':500}}
    assert post(c,path,invalid,actor='coach').status_code==422
    first=post(c,path,body,actor='coach',key='coach-reference-key')
    assert first.status_code==200,first.text
    answer=first.json()['reference_answers'][0]
    assert answer['version']==1 and answer['answer']['spin']=='UNKNOWN'
    assert first.json()['practice_notes'][0]['quiz_item_id']==item['id']
    assert post(c,path,body,actor='coach',key='coach-reference-key').json()==first.json()
    assert post(c,path,body,actor='coach').status_code==409
    second=post(c,path,{**body,'base_version':2,'diagnosis':'MIXED'},actor='coach')
    assert second.status_code==200,second.text
    assert [a['version'] for a in second.json()['reference_answers']]==[2,1]
    reveal=c.get(f"/api/quiz-items/{item['id']}/evidence").json()
    assert len(reveal['reference_answers'])==2
    post(c,'/coach-grants/'+grant['id']+'/revoke',{'base_version':1})
    assert post(c,path,body,actor='coach',key='coach-reference-key').status_code==403
    with factory() as db:
        rows=db.scalars(select(QuizReferenceAnswer).order_by(QuizReferenceAnswer.version)).all()
        assert rows[0].diagnosis=='EXECUTION' and rows[1].diagnosis=='MIXED'
        assert db.scalar(select(QuizAttempt)).scored is False


def test_reference_to_plan_preserves_existing_task_and_versions(bridge):
    c,factory,_=bridge
    item=approved(c);req,_,_=reviewing(c)
    response=post(c,f"/review-requests/{req['id']}/quiz-reference-answers",reference_body(item),actor='coach')
    ref=response.json()['reference_answers'][0]
    body={'report_id':'report','title':'Receive practice','drill':{'description':'Short receive then recover'},
        'retest':{'context':'serve receive','metric':'active_fraction'},'reference_answer_id':ref['id']}
    plan=post(c,'/training-plans/items',body,actor='coach')
    assert plan.status_code==200,plan.text
    assert plan.json()['reference_answer_id']==ref['id'] and plan.json()['locked_by_coach']
    assert post(c,'/training-plans/items',body|{'reference_answer_id':'missing'},actor='coach').status_code==422
    with factory() as db:
        saved=db.get(PlanItem,plan.json()['id'])
        assert saved.version==1 and saved.quiz_reference_answer_id==ref['id']
        assert db.get(Video,'video').state=='READY'
        assert db.get(TimelineActivePointer,'video').timeline_id=='timeline'


def test_reference_rejects_same_timeline_evidence_replacement(bridge):
    c,factory,_=bridge
    item=approved(c);req,_,_=reviewing(c)
    detail=c.get('/api/review-requests/'+req['id'],headers={'X-Test-User':'coach'}).json()
    assert detail['practice_notes'][0]['replay_artifact_id']=='evidence'
    body=reference_body(item)
    with factory.begin() as db:
        db.add(Artifact(id='enriched-evidence',video_id='video',pipeline_run_id='run',stage='REPLAY_ANALYSIS',
            stage_version='test',object_key='enriched-evidence',content_hash='enriched-test'))
        db.flush()
        db.add(ReplayAnalysis(video_id='video',timeline_id='timeline',timeline_version=1,
            engine='local',state='READY',artifact_id='enriched-evidence'))
    path=f"/review-requests/{req['id']}/quiz-reference-answers"
    response=post(c,path,body,actor='coach')
    assert response.status_code==409,response.text
    assert response.json()['error']['code']=='REPLAY_EVIDENCE_CONFLICT'
    refreshed=c.get('/api/review-requests/'+req['id'],headers={'X-Test-User':'coach'}).json()
    assert refreshed['practice_notes'][0]['replay_artifact_id']=='enriched-evidence'
    body['evidence']['replay_artifact_id']='enriched-evidence'
    accepted=post(c,path,body,actor='coach')
    assert accepted.status_code==200,accepted.text
    assert accepted.json()['reference_answers'][0]['evidence']['replay_artifact_id']=='enriched-evidence'
    with factory() as db:
        assert len(db.scalars(select(QuizReferenceAnswer)).all())==1


def test_auto_landing_statistics_are_separate_and_unknowns_excluded(bridge):
    c,_,evidence=bridge
    evidence['strokes']=[{'id':'s1','contact_ms':100,'start_ms':100,'end_ms':600},
                         {'id':'s2','contact_ms':600,'start_ms':600,'end_ms':1000}]
    evidence['placements']=[{'stroke_id':'s1','time_ms':400,'contact_ms':100,'phase':'SERVE','status':'CANDIDATE',
        'table_xy':[.3,.2],'image_xy':None,'depth':'SHORT','lateral':'LEFT','side':'FAR',
        'basis':'PINGPONG3D_MODEL_ESTIMATE'},
        {'stroke_id':'s2','time_ms':800,'contact_ms':600,'phase':'RECEIVE','status':'CANDIDATE','table_xy':None,
         'image_xy':None,'depth':'UNKNOWN','lateral':'UNKNOWN','basis':'PINGPONG3D_MODEL_ESTIMATE'}]
    result=c.get('/api/videos/video/replay-statistics?timeline_version=1')
    assert result.status_code==200,result.text
    stats=result.json()
    assert stats['confirmed_count']==0 and stats['phases']['SERVE']['sample_count']==0
    assert stats['model_estimates']['sample_count']==1 and stats['model_estimates']['unknown_count']==1
    assert stats['model_estimates']['phases']['SERVE']['grid'][0][0]==1
    confirmed=c.put('/api/videos/video/replay-review',json={'base_version':0,'timeline_version':1,
        'corners':None,'analysis_artifact_id':None,'calibration_start_ms':0,'calibration_end_ms':20000,
        'reviews':[{'rally_id':'rally','time_ms':400,'phase':'SERVE','status':'CONFIRMED'}]})
    assert confirmed.status_code==200,confirmed.text
    stats=c.get('/api/videos/video/replay-statistics?timeline_version=1').json()
    assert stats['confirmed_count']==1 and stats['phases']['SERVE']['sample_count']==1


def test_reanalysis_invalidates_confirmations_without_deleting_them(bridge):
    c,factory,_=bridge
    from test_replay import BODY
    assert c.put('/api/videos/video/replay-review',json=BODY).status_code==200
    with factory.begin() as db:
        row=db.get(ReplayReview,'video')
        row.data={**row.data,'reviews':[{'rally_id':'rally','time_ms':266,'phase':'SERVE','status':'CONFIRMED'}]}
        db.add(ReplayAnalysis(video_id='video',timeline_id='timeline',timeline_version=1,
            engine='local',state='READY',artifact_id='evidence'))
    response=c.get(PATH)
    assert response.status_code==200,response.text
    assert response.json()['review']['stale'] and response.json()['review']['reviews']==[]
    assert c.put('/api/videos/video/replay-review',json={**BODY,'base_version':1}).status_code==409
    with factory() as db:
        assert len(db.get(ReplayReview,'video').data['reviews'])==1
