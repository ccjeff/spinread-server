import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
import pytest
from sqlalchemy import select

from test_quizzes import quiz_client
from spinread.core.models import ChapterAnalysis, Job, MediaAsset, Timeline, TimelineActivePointer, Video, utcnow
from spinread.product import chapter_analysis as ca

BODY = dict(chapter_id='chapter-1', title='反手定点', start_ms=1000, end_ms=12000,
            timeline_version=1, annotation_version=0, annotation_id=None, target='FAR', focus='还原',
            budget_micro_usd=150000, consent=True, force=False)
HEADERS = {'Idempotency-Key': 'chapter-request-1', 'X-OpenAI-Key': 'test-secret-never-persist'}
PATH = '/api/videos/video/chapter-analyses'


@pytest.fixture
def prepared(quiz_client):
    client, factory = quiz_client
    with factory.begin() as db:
        db.add(MediaAsset(id='proxy', video_id='video', class_='PROXY', media_version=1, object_key='fake/proxy.mp4',
                          content_hash='hash', byte_size=123, status='ACTIVE'))
    yield client, factory
    with ca._lock: ca._vault.clear()


def submit(client, **changes):
    return client.post(PATH, json=BODY | changes, headers=HEADERS)


def test_creation_idempotency_and_secret_not_in_storage(prepared):
    c, factory = prepared
    one = submit(c)
    assert one.status_code == 202, one.text
    assert submit(c).json()['id'] == one.json()['id']
    assert submit(c, focus='changed').status_code == 409
    assert HEADERS['X-OpenAI-Key'] not in one.text
    with factory() as db:
        rows = db.scalars(select(ChapterAnalysis)).all()
        jobs = db.scalars(select(Job)).all()
        assert len(rows) == len(jobs) == 1
        assert jobs[0].max_attempts == 1
        assert HEADERS['X-OpenAI-Key'] not in json.dumps(rows[0].snapshot)
        assert list(jobs[0].payload) == ['analysis_id']


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
    assert result['id'] not in ca._vault
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
    assert result['id'] not in ca._vault


def test_windows_and_invalid_detail_requests():
    assert ca.windows(1000,2000) == [(1000,2000)]
    assert ca.windows(1000,101000) == [(1000,7000),(48000,54000),(95000,101000)]
    requests=[ca.DetailRequest(start_ms=a,end_ms=b,reason='check') for a,b in [(0,2000),(1000,6000),(2000,3000),(2500,3500),(4000,5000),(6000,7000)]]
    accepted,rejected=ca.detail_windows(requests,1000,12000)
    assert accepted==[(2000,3000),(4000,5000)] and len(rejected)==4


def test_evidence_gate_rejects_unseen_frames_and_inconsistent_abstention():
    result = ca.Assessment(summary='观察',assessability='PARTIAL',observations=[ca.Observation(dimension='RECOVERY',kind='IMPROVEMENT',observation='可见还原过程',evidence_ids=['invented'],suggestion='练习还原')],limitations=[])
    with pytest.raises(ca.AnalysisError): ca.validate_assessment(result,[{'id':'W1_00'}])
    result.observations[0].evidence_ids=['W1_00']
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
    assert ca.storyboard(frames)[0].startswith('data:image/jpeg;base64,')


def test_worker_runs_overview_detail_final_and_preserves_manifest(prepared,monkeypatch):
    c,factory=prepared
    item=submit(c).json()
    def extract(path,a,b,fps,prefix):
        return [({'id':prefix+'_00','timestamp_ms':a,'window_id':prefix},np.zeros((100,200,3),np.uint8))]
    monkeypatch.setattr(ca,'extract_frames',extract)
    calls=[]
    def model(key,messages,kind,row,db):
        assert key==HEADERS['X-OpenAI-Key']
        calls.append(messages)
        if kind is ca.Overview:
            return ca.Overview(preliminary_observations=['还原需补看'],detail_requests=[ca.DetailRequest(start_ms=2000,end_ms=3500,reason='移动')])
        return ca.Assessment(summary='本次样本显示还原过程',assessability='PARTIAL',observations=[ca.Observation(dimension='RECOVERY',kind='NEUTRAL',observation='观察动作还原',evidence_ids=['D1_00'],suggestion='和教练复核还原时机')],limitations=['仅抽样观察'])
    monkeypatch.setattr(ca,'call_model',model)
    with factory() as db:
        job=db.scalar(select(Job))
        assert ca.run_analysis(db,SimpleNamespace(),SimpleNamespace(download_file=lambda *_:None),job)=='done'
        db.expire_all();row=db.get(ChapterAnalysis,item['id'])
        assert row.status=='SUCCEEDED',row.error
        assert len(row.manifest)==3 and len(row.coverage)==3
        assert row.result['review_status']=='AI_DRAFT'
        assert row.snapshot['target']=='FAR'
        assert len(calls)==2
    assert item['id'] not in ca._vault


def test_worker_missing_key_fails_without_provider_calls(prepared,monkeypatch):
    c,factory=prepared;item=submit(c).json();ca.forget_key(item['id'])
    model=Mock();monkeypatch.setattr(ca,'call_model',model)
    with factory() as db:
        ca.run_analysis(db,SimpleNamespace(),None,db.scalar(select(Job)))
        row=db.get(ChapterAnalysis,item['id'])
        assert row.status=='FAILED' and row.error['code']=='KEY_EXPIRED'
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
        ca.run_analysis(db,SimpleNamespace(),SimpleNamespace(download_file=lambda *_:None),db.scalar(select(Job)))
        db.expire_all(); row=db.get(ChapterAnalysis,item['id'])
        assert row.status=='CANCELLED' and row.result is None
    assert calls==[ca.Overview]


def test_key_expiry_clears_memory():
    with ca._lock: ca._vault['expired']=(0,'secret')
    ca.purge_expired_keys()
    assert 'expired' not in ca._vault
