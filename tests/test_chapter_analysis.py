import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from sqlalchemy import select
from pydantic import SecretStr
from spinread.api.routers import chapter_analyses as router
from spinread.product.chapter_contract import build_input
from spinread.core.models import TrainingAnnotationRevision, CoachGrant, ConsentGrant, User

from test_quizzes import quiz_client
from spinread.core.models import ChapterAnalysis, Job, MediaAsset, Timeline, TimelineActivePointer, Video, utcnow
from spinread.product import chapter_analysis as ca


def plan(evidence='W1_00'):
    return ca.PracticePlan(goal='还原准备', observation_indices=[0], evidence_ids=[evidence], drill='低速衔接',
        steps=['击球后回准备位'], dosage='2组×8次，组间休息45秒', cue='打完回位', success_criteria='连续5次及时准备', retest='同节奏复测')

BODY = dict(chapter_id='chapter-1', title='反手定点', start_ms=1000, end_ms=12000,
            timeline_version=1, annotation_version=0, annotation_id=None, target='FAR', focus='还原',
            comparison=None, force=False)
HEADERS = {'Idempotency-Key': 'chapter-request-1'}
SECRET = 'test-secret-never-persist'
SETTINGS = SimpleNamespace(openai_api_key=SecretStr(SECRET))
PATH = '/api/videos/video/chapter-analyses'


@pytest.fixture
def prepared(quiz_client, monkeypatch):
    monkeypatch.setattr(router, 'get_settings', lambda: SETTINGS)
    client, factory = quiz_client
    with factory.begin() as db:
        db.add(MediaAsset(id='proxy', video_id='video', class_='PROXY', media_version=1, object_key='fake/proxy.mp4',
                          content_hash='hash', byte_size=123, status='ACTIVE'))
    yield client, factory



def submit(client, **changes):
    return client.post(PATH, json=BODY | changes, headers=HEADERS)


def test_creation_idempotency_and_secret_not_in_storage(prepared):
    c, factory = prepared
    one = submit(c)
    assert one.status_code == 202, one.text
    assert submit(c).json()['id'] == one.json()['id']
    assert submit(c, focus='changed').status_code == 409
    assert SECRET not in one.text
    assert not {'coverage', 'usage', 'budget_micro_usd'} & one.json().keys()
    assert set(one.json()['snapshot']) == {'title', 'target', 'focus'}
    with factory() as db:
        rows = db.scalars(select(ChapterAnalysis)).all()
        jobs = db.scalars(select(Job)).all()
        assert len(rows) == len(jobs) == 1
        assert jobs[0].max_attempts == 1
        assert SECRET not in json.dumps(rows[0].snapshot)
        assert list(jobs[0].payload) == ['analysis_id']


def test_key_is_server_configuration_not_browser_input(prepared, monkeypatch):
    c, _ = prepared
    monkeypatch.setattr(SETTINGS, 'openai_api_key', SecretStr(''))
    response = submit(c)
    assert response.status_code == 503
    assert 'key' not in response.text


def annotated_pair(factory, *, owner='owner', actions=None):
    actions = actions or [{'hand': 'BACKHAND', 'stroke': '拉球', 'incoming_spin': 'TOPSPIN', 'movement': ''}]
    segment = {'id': 'current', 'start_ms': 1000, 'end_ms': 12000, 'title': '反手拉球',
        'feeding': 'MULTIBALL', 'movement': 'FIXED', 'target': 'FAR', 'actions': actions, 'notes': ''}
    old = segment | {'id': 'previous', 'target': 'NEAR'}
    with factory.begin() as db:
        db.add(Video(id='old', owner_id=owner, filename='old.mp4', state='READY', duration_ms=20000))
        db.flush()
        db.add(Timeline(id='old-tl', video_id='old', version=1, state='PUBLISHED'))
        db.add(TrainingAnnotationRevision(id='current-rev',video_id='video',version=1,author_id='owner',segments=[segment]))
        db.add(TrainingAnnotationRevision(id='old-rev',video_id='old',version=1,author_id=owner,segments=[old]))
        db.add(MediaAsset(id='old-proxy',video_id='old',class_='PROXY',media_version=1,object_key='old.mp4',content_hash='old-hash',byte_size=100,status='ACTIVE'))
        db.flush()
        db.add(TimelineActivePointer(video_id='old', timeline_id='old-tl'))
    return segment


def comparison_body():
    return {'annotation_id': 'current', 'annotation_version': 1,
        'comparison': {'video_id': 'old', 'annotation_id': 'previous', 'annotation_version': 1, 'same_player': True}}


def test_contract_uses_user_actions_and_resolves_target_override(prepared):
    c, factory = prepared
    annotated_pair(factory)
    response=submit(c, annotation_id='current', annotation_version=1)
    assert response.status_code == 202, response.text
    with factory() as db:
        contract=db.get(ChapterAnalysis,response.json()['id']).snapshot['input']
    assert contract['training_intent']['source']=='USER'
    assert contract['training_intent']['feeding']=='MULTIBALL'
    assert '反手拉球' in contract['training_intent']['action_sequence'][0]
    assert any('旋转标签' in x for x in contract['known_limitations'])
    contract=build_input(video_id='video',chapter_id='c',start_ms=1000,end_ms=12000,timeline_version=1,
        annotation_version=1,target='NEAR',annotation=annotated_segment(),focus='',manifest_id='m')
    assert contract.target.position=='NEAR' and contract.training_intent.source=='UNKNOWN'
    assert contract.training_intent.action_sequence==[]


def annotated_segment():
    return {'target':'FAR','feeding':'MULTIBALL','actions':[{'hand':'BACKHAND','stroke':'拉球','incoming_spin':'TOPSPIN'}]}


def test_candidates_require_matching_actions_and_owner(prepared):
    c,factory=prepared;annotated_pair(factory)
    url='/api/videos/video/chapter-comparison-candidates?annotation_id=current&target=FAR'
    assert c.get(url).json()['items'][0]['video_id']=='old'
    assert c.get(url.replace('FAR','NEAR')).json()['items']==[]
    assert c.get(url,headers={'X-Test-User':'other'}).status_code==403
    with factory.begin() as db:
        rev=db.get(TrainingAnnotationRevision,'old-rev')
        seg=rev.segments[0].copy(); seg['actions']=[]; rev.segments=[seg]
    assert c.get(url).json()['items']==[]
    assert submit(c,**comparison_body()).status_code==409


def test_cross_owner_and_unconfirmed_comparison_rejected(prepared):
    c,factory=prepared;annotated_pair(factory,owner='other')
    assert submit(c,**comparison_body()).status_code==403
    body=comparison_body();body['comparison']['same_player']=False
    assert submit(c,**body).status_code==422


def test_worker_reads_both_videos_and_validates_two_sided_evidence(prepared,monkeypatch):
    c,factory=prepared;annotated_pair(factory)
    response=submit(c,**comparison_body());assert response.status_code==202,response.text
    item=response.json(); downloads=[]; calls=[]
    monkeypatch.setattr(ca,'extract_frames',lambda p,a,b,f,prefix:[({'id':prefix+'_00','timestamp_ms':a,'window_id':prefix},np.zeros((100,200,3),np.uint8))])
    def model(key,messages,kind,row,db):
        payload=json.loads(messages[1]['content'][0]['text'].split('\n')[0]);calls.append(payload)
        assert payload['previous']['scope']['video_id']=='old'
        assert payload['previous']['target']['position']=='NEAR'
        assert payload['input']['target']['position']=='FAR'
        assert {f['role'] for f in payload['frames']}=={'CURRENT','PREVIOUS'}
        if kind is ca.Overview: return ca.Overview(preliminary_observations=[],detail_requests=[])
        return assessment(comparison=True)
    monkeypatch.setattr(ca,'call_model',model)
    with factory() as db:
        ca.run_analysis(db,SETTINGS,SimpleNamespace(download_file=lambda source,path:downloads.append(source)),db.scalar(select(Job)))
        db.expire_all();row=db.get(ChapterAnalysis,item['id'])
        assert row.status=='SUCCEEDED',row.error
    assert set(downloads)=={'fake/proxy.mp4','old.mp4'} and len(calls)==2
    public=c.get(f"{PATH}/{item['id']}").json()
    assert public['comparison_source']['video_id']=='old'
    assert public['result']['practice_plan'][0]['success_criteria']
    # Deletion after analysis invalidates the prior evidence without leaking it.
    with factory.begin() as db: db.get(Video,'old').deleted_at=utcnow()
    public=c.get(f"{PATH}/{item['id']}").json()
    assert public['stale'] and public['comparison_source'] is None
    assert public['result'] is None
    assert all(f.get('role')!='PREVIOUS' for f in public['manifest'])


def assessment(comparison=False):
    return ca.Assessment(summary='可见还原',assessability='PARTIAL',observations=[ca.Observation(
        dimension='RECOVERY',kind='IMPROVEMENT',observation='准备略晚',evidence_ids=['W1_00'],suggestion='还原练习')],
        limitations=[],practice_plan=[plan()],comparison=ca.Comparison(comparability='COMPARABLE',summary='还原更及时',limitations=[],
            changes=[ca.ComparisonChange(dimension='RECOVERY',change='IMPROVED',observation='本次更早准备',current_evidence_ids=['W1_00'],previous_evidence_ids=['P1_00'])]) if comparison else None)


def test_actionable_plan_and_comparison_gates():
    frames=[{'id':'W1_00','role':'CURRENT'},{'id':'P1_00','role':'PREVIOUS'}]
    result=assessment(True);ca.validate_assessment(result,frames,True)
    result.comparison.changes[0].previous_evidence_ids=['W1_00']
    with pytest.raises(ca.AnalysisError):ca.validate_assessment(result,frames,True)
    result=assessment(True);result.comparison.comparability='LIMITED'
    with pytest.raises(ca.AnalysisError):ca.validate_assessment(result,frames,True)
    result=assessment();result.practice_plan[0].success_criteria=' '
    with pytest.raises(ca.AnalysisError):ca.validate_assessment(result,frames)
    result=assessment();result.practice_plan[0].observation_indices=[10]
    with pytest.raises(ca.AnalysisError):ca.validate_assessment(result,frames)
    result=assessment();result.practice_plan=[]
    with pytest.raises(ca.AnalysisError):ca.validate_assessment(result,frames)


def test_output_schema_requires_every_field_and_disallows_extra_properties():
    schema=ca.Assessment.model_json_schema()
    for obj in [schema,*schema['$defs'].values()]:
        if obj.get('type')=='object':
            assert obj['additionalProperties'] is False
            assert set(obj['required'])==set(obj['properties'])


@pytest.mark.parametrize('change', [{'consent': False}, {'target':'UNSPECIFIED'}, {'start_ms':11999}, {'end_ms':21000}, {'timeline_version':2}, {'annotation_version':1}, {'annotation_id':'missing'}, {'budget_micro_usd':1}])
def test_invalid_or_stale_input(prepared, change):
    c, _ = prepared
    assert submit(c, **change).status_code in (409, 422)


def test_authorization_deletion_and_cancel(prepared):
    c, factory = prepared
    assert c.post(PATH, json=BODY, headers=HEADERS | {'X-Test-User':'other'}).status_code == 403
    result = submit(c).json()
    assert c.get(PATH, headers={'X-Test-User':'other'}).status_code == 403
    assert c.get(f"{PATH}/{result['id']}", headers={'X-Test-User':'other'}).status_code == 403
    cancelled = c.post(f"{PATH}/{result['id']}/cancel")
    assert cancelled.json()['status'] == 'CANCELLED'
    with factory.begin() as db: db.get(Video,'video').deleted_at = utcnow()
    assert c.get(PATH).status_code == 404


def test_stale_history_cache_and_expiry(prepared):
    c, factory = prepared
    result = submit(c).json()
    with factory.begin() as db:
        row = db.get(ChapterAnalysis, result['id'])
        row.status = 'SUCCEEDED'
    cached = c.post(PATH, json=BODY, headers={'Idempotency-Key':'different-request'})
    assert cached.json()['id'] == result['id']
    with factory.begin() as db:
        row = db.get(ChapterAnalysis, result['id'])
        row.status='QUEUED'; row.updated_at=utcnow()-timedelta(minutes=16)
        db.add(Timeline(id='timeline-new',video_id='video',version=2,state='PUBLISHED'));db.flush()
        db.get(TimelineActivePointer,'video').timeline_id='timeline-new'
    out = c.get(PATH).json()['items'][0]
    assert out['stale'] and out['status'] == 'FAILED'


def test_windows_and_invalid_detail_requests():
    assert ca.windows(1000,2000) == [(1000,2000)]
    assert ca.windows(1000,101000) == [(1000,7000),(48000,54000),(95000,101000)]
    requests=[ca.DetailRequest(start_ms=a,end_ms=b,reason='check') for a,b in [(0,2000),(1000,6000),(2000,3000),(2500,3500),(4000,5000),(6000,7000)]]
    accepted,rejected=ca.detail_windows(requests,1000,12000)
    assert accepted==[(2000,3000),(4000,5000)] and len(rejected)==4


def test_evidence_gate_rejects_unseen_frames_and_inconsistent_abstention():
    result = ca.Assessment(summary='观察',assessability='PARTIAL',observations=[ca.Observation(dimension='RECOVERY',kind='IMPROVEMENT',observation='可见还原过程',evidence_ids=['invented'],suggestion='练习还原')],limitations=[], practice_plan=[plan('invented')], comparison=None)
    with pytest.raises(ca.AnalysisError): ca.validate_assessment(result,[{'id':'W1_00'}])
    result.observations[0].evidence_ids=['W1_00']
    result.practice_plan=[plan('W1_00')]
    ca.validate_assessment(result,[{'id':'W1_00'}])
    result.assessability='NOT_ASSESSABLE'
    with pytest.raises(ca.AnalysisError): ca.validate_assessment(result,[{'id':'W1_00'}])


def test_real_frame_sampling_and_storyboard(tmp_path):
    path=tmp_path/'source.mp4'
    out=cv2.VideoWriter(str(path),cv2.VideoWriter_fourcc(*'mp4v'),30,(320,180))
    for i in range(90): out.write(np.full((180,320,3),i*2,np.uint8))
    out.release()
    frames=ca.extract_frames(path,1000,2000,4,'W1')
    assert len(frames)==4
    assert all(1000 <= f['timestamp_ms'] < 2000 for f,_ in frames)
    assert len(ca.storyboard(frames))==1
    assert frames[0][0]['source_size']==[320,180]
    assert frames[0][0]['crop']==[0,0,320,180]
    assert frames[3][0]['storyboard']['row']==1
    assert frames[3][0]['storyboard']['column']==0
    assert ca.storyboard(frames)[0].startswith('data:image/jpeg;base64,')


def test_worker_runs_overview_detail_final_and_preserves_manifest(prepared,monkeypatch):
    c,factory=prepared
    item=submit(c).json()
    def extract(path,a,b,fps,prefix):
        return [({'id':prefix+'_00','timestamp_ms':a,'window_id':prefix},np.zeros((100,200,3),np.uint8))]
    monkeypatch.setattr(ca,'extract_frames',extract)
    calls=[]
    def model(key,messages,kind,row,db):
        assert key==SECRET
        calls.append(messages)
        if kind is ca.Overview:
            return ca.Overview(preliminary_observations=['还原需补看'],detail_requests=[ca.DetailRequest(start_ms=2000,end_ms=3500,reason='移动')])
        return ca.Assessment(summary='本次样本显示还原过程',assessability='PARTIAL',observations=[ca.Observation(dimension='RECOVERY',kind='NEUTRAL',observation='观察动作还原',evidence_ids=['D1_00'],suggestion='和教练复核还原时机')],limitations=[], practice_plan=[plan('D1_00')], comparison=None)
    monkeypatch.setattr(ca,'call_model',model)
    with factory() as db:
        job=db.scalar(select(Job))
        assert ca.run_analysis(db,SETTINGS,SimpleNamespace(download_file=lambda *_:None),job)=='done'
        db.expire_all();row=db.get(ChapterAnalysis,item['id'])
        assert row.status=='SUCCEEDED',row.error
        assert len(row.manifest)==3 and len(row.coverage)==3
        assert row.result['review_status']=='AI_DRAFT'
        assert row.snapshot['target']=='FAR'
        assert len(calls)==2
        payload=json.loads(calls[0][1]['content'][0]['text'].split('\n')[0])
        assert payload['input']==row.snapshot['input']
        assert payload['input']['schema_version']=='chapter-analysis-v1'
        assert payload['input']['scope']['video_id']=='video'
        assert payload['input']['scope']['chapter_id']=='chapter-1'
        assert payload['input']['target']=={'position':'FAR','source':'USER'}
        assert payload['input']['sample_manifest_id']==item['id']+':samples'
        assert 'media_hash' not in calls[0][1]['content'][0]['text']


def test_worker_missing_key_fails_without_provider_calls(prepared,monkeypatch):
    c,factory=prepared;item=submit(c).json()
    monkeypatch.setattr(SETTINGS, 'openai_api_key', SecretStr(''))
    model=Mock();monkeypatch.setattr(ca,'call_model',model)
    with factory() as db:
        ca.run_analysis(db,SETTINGS,None,db.scalar(select(Job)))
        row=db.get(ChapterAnalysis,item['id'])
        assert row.status=='FAILED' and row.error['code']=='SERVICE_UNAVAILABLE'
    model.assert_not_called()


def test_provider_redaction_budget_and_recorded_usage():
    row=SimpleNamespace(usage=[],budget_micro_usd=1)
    with pytest.raises(ca.AnalysisError,match='BUDGET_EXCEEDED'):
        ca.call_model('secret', [{'role':'user','content':'test'}],ca.Overview,row,Mock())


def test_provider_errors_are_redacted_and_not_retried(monkeypatch):
    response=SimpleNamespace(status_code=401)
    transport=Mock();transport.post.return_value=response
    client=Mock();client.__enter__=Mock(return_value=transport);client.__exit__=Mock(return_value=False)
    monkeypatch.setattr(ca.httpx,'Client',lambda **_:client)
    with pytest.raises(ca.AnalysisError) as caught:
        ca.call_model('private-key',[{'role':'user','content':'test'}],ca.Overview,SimpleNamespace(usage=[],budget_micro_usd=150000),Mock())
    assert caught.value.code=='INVALID_KEY'
    assert 'private-key' not in str(caught.value) + caught.value.public_message
    assert transport.post.call_count==1


def test_paid_usage_survives_invalid_provider_output(monkeypatch):
    response=SimpleNamespace(status_code=200,json=lambda:{'usage':{'prompt_tokens':100,'completion_tokens':20},'choices':[{'finish_reason':'stop','message':{'content':'not-json'}}]})
    transport=Mock();transport.post.return_value=response
    client=Mock();client.__enter__=Mock(return_value=transport);client.__exit__=Mock(return_value=False)
    monkeypatch.setattr(ca.httpx,'Client',lambda **_:client)
    row=SimpleNamespace(usage=[],budget_micro_usd=150000);db=Mock()
    with pytest.raises(ca.AnalysisError,match='INVALID_OUTPUT'):
        ca.call_model('private-key',[{'role':'user','content':'test'}],ca.Overview,row,db)
    assert row.usage==[{'input_tokens':100,'output_tokens':20,'cost_micro_usd':72}]
    db.commit.assert_called_once()


def test_cancel_during_provider_call_does_not_publish_or_continue(prepared,monkeypatch):
    c,factory=prepared;item=submit(c).json()
    monkeypatch.setattr(ca,'extract_frames',lambda p,a,b,f,prefix:[({'id':prefix+'_00','timestamp_ms':a,'window_id':prefix},np.zeros((100,200,3),np.uint8))])
    calls=[]
    def model(key,messages,kind,row,db):
        calls.append(kind)
        with factory.begin() as other: other.get(ChapterAnalysis,row.id).status='CANCELLED'
        return ca.Overview(preliminary_observations=[],detail_requests=[])
    monkeypatch.setattr(ca,'call_model',model)
    with factory() as db:
        ca.run_analysis(db,SETTINGS,SimpleNamespace(download_file=lambda *_:None),db.scalar(select(Job)))
        db.expire_all(); row=db.get(ChapterAnalysis,item['id'])
        assert row.status=='CANCELLED' and row.result is None
    assert calls==[ca.Overview]

def test_output_schema_limits_citations_to_supplied_frame_roles():
    manifest=[{'id':'W1_00','role':'CURRENT'}, {'id':'P1_00','role':'PREVIOUS'}]
    schema=ca.output_schema(ca.Assessment,manifest)
    assert schema['$defs']['Observation']['properties']['evidence_ids']['items']['enum']==['W1_00']
    assert schema['$defs']['ComparisonChange']['properties']['previous_evidence_ids']['items']['enum']==['P1_00']
    schema=ca.output_schema(ca.Assessment,manifest[:1])
    assert schema['properties']['comparison']=={'type':'null'}


def test_coach_current_video_access_does_not_share_history(prepared):
    c,factory=prepared;annotated_pair(factory)
    item=submit(c,**comparison_body()).json()
    with factory.begin() as db:
        row=db.get(ChapterAnalysis,item['id']);row.status='SUCCEEDED';row.result=assessment(True).model_dump()
        row.manifest=[{'id':'W1_00','role':'CURRENT','timestamp_ms':1000}, {'id':'P1_00','role':'PREVIOUS','timestamp_ms':1000}]
        db.get(User,'other').role='COACH'
        db.add(CoachGrant(player_id='owner',coach_id='other'))
        db.add(ConsentGrant(video_id='video',subject_user_id='owner',purpose='COACH_VIEW',granted_to='other'))
    path=f"{PATH}/{item['id']}"
    public=c.get(path,headers={'X-Test-User':'other'}).json()
    assert public['result'] is None and public['comparison_source'] is None and public['error']
    assert all(f['role']=='CURRENT' for f in public['manifest'])
    with factory.begin() as db:
        db.add(ConsentGrant(video_id='old',subject_user_id='owner',purpose='COACH_VIEW',granted_to='other'))
    public=c.get(path,headers={'X-Test-User':'other'}).json()
    assert public['result']['comparison'] and public['comparison_source']['video_id']=='old'
    assert 'limitations' not in public['result']


def test_history_changes_invalidate_selection_and_cache(prepared):
    c,factory=prepared;annotated_pair(factory)
    first=submit(c,**comparison_body()).json()
    with factory.begin() as db:
        db.get(ChapterAnalysis,first['id']).status='SUCCEEDED'
        old=db.get(TrainingAnnotationRevision,'old-rev')
        db.add(TrainingAnnotationRevision(id='old-rev2',video_id='old',version=2,author_id='owner',segments=old.segments))
    body=BODY|comparison_body()
    headers={'Idempotency-Key':'history-change-test'}
    assert c.post(PATH,json=body,headers=headers).status_code==409
    body['comparison']['annotation_version']=2
    second=c.post(PATH,json=body,headers=headers)
    assert second.status_code==202 and second.json()['id']!=first['id']
    assert c.get(f"{PATH}/{first['id']}").json()['stale']


def test_history_deleted_before_worker_prevents_paid_calls(prepared,monkeypatch):
    c,factory=prepared;annotated_pair(factory)
    item=submit(c,**comparison_body()).json()
    with factory.begin() as db: db.get(Video,'old').deleted_at=utcnow()
    model=Mock();monkeypatch.setattr(ca,'call_model',model)
    with factory() as db:
        ca.run_analysis(db,SETTINGS,None,db.scalar(select(Job)))
        assert db.get(ChapterAnalysis,item['id']).status=='FAILED'
    model.assert_not_called()
