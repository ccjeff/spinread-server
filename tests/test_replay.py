import json
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from test_quizzes import quiz_client
from spinread.api.main import app
from spinread.api.deps import get_s3
from spinread.core.models import Artifact, PipelineRun, TimelineItem, Timeline, TimelineActivePointer, Video, utcnow
from spinread.pipeline.stages.events_stage import EventsStage
from spinread.config import Settings
from pingpong_training.analysis.replay import replay_evidence


@pytest.fixture
def replay_client(quiz_client):
    c,factory=quiz_client
    evidence=replay_evidence(None,[],20000)
    evidence['frames']=[[200,.5,.4],[233,.5,.42],[266,.5,.44],[299,.5,.42],[332,.5,.4]]
    with factory.begin() as db:
        db.add(PipelineRun(id='run',video_id='video',pipeline_version='1.2.0',state='SUCCEEDED'));db.flush()
        db.add(Artifact(id='evidence',video_id='video',pipeline_run_id='run',stage='EVENTS',stage_version='test',object_key='evidence',content_hash='test'))
        db.add(TimelineItem(id='rally',timeline_id='timeline',type='RALLY',start_ms=0,end_ms=2000,attributes={'replay_artifact_id':'evidence'}));db.flush()
        for i,t in enumerate([0,600,1000]):db.add(TimelineItem(id=f'hit{i}',timeline_id='timeline',parent_id='rally',type='HIT_CANDIDATE',start_ms=t,end_ms=t+40))
    app.dependency_overrides[get_s3]=lambda:SimpleNamespace(get_bytes=lambda key:json.dumps({'video_id':'video','replay':evidence}).encode())
    yield c,factory,evidence


PATH='/api/videos/video/rallies/rally/replay?timeline_version=1'
BODY={'base_version':0,'timeline_version':1,'corners':[[.1,.1],[.9,.1],[.9,.9],[.1,.9]],
      'calibration_start_ms':0,'calibration_end_ms':20000,'fixed_camera':True,'reviews':[]}


def test_replay_authorization_version_and_half_open_range(replay_client):
    c,factory,_=replay_client
    r=c.get(PATH);assert r.status_code==200,r.text
    assert len(r.json()['contacts'])==3
    assert c.get(PATH,headers={'X-Test-User':'other'}).status_code in (403,404)
    assert c.get(PATH.replace('version=1','version=2')).status_code==409
    assert c.get(PATH+'&start_ms=2000').status_code==422
    with factory.begin() as db:db.get(Video,'video').deleted_at=utcnow()
    assert c.get(PATH).status_code==404


def test_calibration_confirmed_stats_conflicts_and_recalibration(replay_client):
    c,factory,_=replay_client
    r=c.put('/api/videos/video/replay-review',json=BODY);assert r.status_code==200,r.text
    replay=c.get(PATH).json();assert len(replay['placements'])==1
    assert replay['statistics']['confirmed_count']==0
    review={'rally_id':'rally','time_ms':266,'phase':'SERVE','status':'CONFIRMED'}
    body=BODY|{'base_version':1,'reviews':[review]}
    r=c.put('/api/videos/video/replay-review',json=body);assert r.status_code==200,r.text
    stats=c.get('/api/videos/video/replay-statistics?timeline_version=1').json()
    assert stats['phases']['SERVE']['sample_count']==1
    assert c.put('/api/videos/video/replay-review',json=body).status_code==409
    assert c.put('/api/videos/video/replay-review',json=body|{'base_version':2,'corners':[[0,0],[1,0],[1,1],[0,1]]}).status_code==422
    assert c.put('/api/videos/video/replay-review',json=body|{'base_version':2,'reviews':[review|{'time_ms':999}]}).status_code==422
    with factory.begin() as db:
        db.add(Timeline(id='next',video_id='video',version=2,state='PUBLISHED'));db.flush()
        db.get(TimelineActivePointer,'video').timeline_id='next'
    stats=c.get('/api/videos/video/replay-statistics?timeline_version=2').json()
    assert stats['stale'] is True and stats['confirmed_count']==0


def test_audio_fallback_does_not_claim_pose_or_visual_support():
    ctx=SimpleNamespace(prior_artifacts={'RALLY':1},load_artifact_json=lambda _: {'rallies':[{'start_ms':0,'end_ms':1000,'hits_ms':[100,100,1000]}]},
                        video=SimpleNamespace(id='v',duration_ms=1000),settings=Settings(blurball_weights='',pose_weights=''))
    result=EventsStage().run(ctx)
    assert len(result.artifact_json['events'])==1
    assert not result.artifact_json['events'][0]['visual_supported']
    assert result.artifact_json['replay']['frames']==[]


def test_legacy_timeline_has_explicit_empty_evidence(replay_client):
    c,factory,_=replay_client
    with factory.begin() as db:db.get(TimelineItem,'rally').attributes={}
    data=c.get(PATH).json()
    assert data['evidence_available'] is False
    assert data['pose'] is None and data['frames']==[]
