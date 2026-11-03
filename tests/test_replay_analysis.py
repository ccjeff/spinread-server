"""Supplemental jobs preserve source timelines, records and previous evidence."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_quizzes import quiz_client
from spinread.api.main import app
from spinread.config import Settings, get_settings
from spinread.core.models import (Artifact, Job, MediaAsset, PipelineRun, ReplayAnalysis,
    Timeline, TimelineActivePointer, TimelineItem, Video)
from spinread.product import replay_analysis as service
from pingpong_training.analysis.replay import replay_evidence


class Store:
    def __init__(self):
        self.objects = {"original": b"unchanged"}
    def download_file(self, key, path):
        Path(path).write_bytes(self.objects[key])
    def put_bytes(self, key, data, content_type=None):
        self.objects[key] = data
    def get_bytes(self, key):
        return self.objects[key]


@pytest.fixture
def analysis_client(quiz_client, tmp_path):
    c, factory = quiz_client
    weights = tmp_path / "weights"; weights.touch()
    settings = Settings(_env_file=None, pose_weights=str(weights), tmp_dir=str(tmp_path))
    app.dependency_overrides[get_settings] = lambda: settings
    with factory.begin() as db:
        db.add(PipelineRun(id="run", video_id="video", pipeline_version="legacy", state="SUCCEEDED"))
        db.add(MediaAsset(id="original", video_id="video", class_="ORIGINAL", object_key="original",
            content_hash="old-source-hash", byte_size=9, status="ACTIVE"))
        db.add(TimelineItem(id="rally", timeline_id="timeline", type="RALLY", start_ms=0, end_ms=2000,
            attributes={"manual_note": "preserve"}))
    return c, factory, settings


def enqueue(c, key="analysis-request-1"):
    r = c.post("/api/videos/video/replay-analysis", json={"timeline_version": 1, "engine": "local"},
               headers={"Idempotency-Key": key})
    assert r.status_code == 202, r.text
    return r.json()


def test_analysis_is_authorized_idempotent_and_non_destructive(analysis_client, monkeypatch):
    c, factory, settings = analysis_client
    first = enqueue(c)
    assert enqueue(c)["id"] == first["id"]
    assert c.post("/api/videos/video/replay-analysis", json={"timeline_version":1},
        headers={"Idempotency-Key":"other-user-123", "X-Test-User":"other"}).status_code == 403
    assert c.get("/api/videos/video/replay-analysis?timeline_version=2").status_code == 409
    result = replay_evidence(None, [], 20000); result["frames"] = [[100,.2,.3]]
    monkeypatch.setattr(service, "_local", lambda *args: (result, []))
    store = Store()
    with factory() as db:
        job = db.scalar(select(Job).where(Job.kind == "REPLAY_ANALYSIS"))
        assert service.run_replay_analysis(db,settings,store,job) == "done"
        row = db.get(ReplayAnalysis, first["id"])
        assert row.state == "READY" and row.artifact_id
        assert db.get(Video,"video").state == "READY"
        assert db.get(TimelineActivePointer,"video").timeline_id == "timeline"
        assert db.get(TimelineItem,"rally").attributes == {"manual_note":"preserve"}
        assert db.get(MediaAsset,"original").content_hash == "old-source-hash"
        assert store.objects["original"] == b"unchanged"
        assert service.analysis_evidence(db,store,db.get(Video,"video"),"timeline",{})["frames"] == result["frames"]
        assert service.analysis_evidence(db,store,db.get(Video,"video"),"unrelated",{}) is None
        aid = row.artifact_id
        service.run_replay_analysis(db,settings,store,job)
        assert row.artifact_id == aid
    assert not (Path(settings.tmp_dir)/"replay-analysis"/first["id"]).exists()


def test_failure_keeps_previous_success_and_safe_error(analysis_client, monkeypatch):
    c, factory, settings = analysis_client
    evidence = replay_evidence(None, [], 20000); evidence["frames"] = [[100,.2,.3]]
    monkeypatch.setattr(service,"_local",lambda *args:(evidence,[]))
    first=enqueue(c);store=Store()
    with factory() as db:
        service.run_replay_analysis(db,settings,store,db.scalar(select(Job)))
    second=enqueue(c,"analysis-request-2")
    def fail(*args):
        raise RuntimeError("private internal path or token must not appear")
    monkeypatch.setattr(service,"_local",fail)
    with factory() as db:
        job=db.scalar(select(Job).where(Job.payload["analysis_id"].as_string()==second["id"]))
        service.run_replay_analysis(db,settings,store,job)
        assert db.get(ReplayAnalysis,second["id"]).state=="FAILED"
        assert service.analysis_evidence(db,store,db.get(Video,"video"),"timeline",{})["frames"]==evidence["frames"]
        assert db.get(ReplayAnalysis,first["id"]).state=="READY"
    status=c.get("/api/videos/video/replay-analysis?timeline_version=1").json()
    assert status["error_code"]=="ANALYSIS_FAILED" and "private" not in json.dumps(status)


def test_timeline_edit_during_analysis_does_not_publish(analysis_client, monkeypatch):
    c,factory,settings=analysis_client
    result=replay_evidence(None,[],20000);result["frames"]=[[100,.2,.3]]
    def edit(*args):
        with factory.begin() as db:
            db.add(Timeline(id="next",video_id="video",version=2,state="PUBLISHED"));db.flush()
            db.get(TimelineActivePointer,"video").timeline_id="next"
        return result,[]
    monkeypatch.setattr(service,"_local",edit)
    queued=enqueue(c)
    with factory() as db:
        service.run_replay_analysis(db,settings,Store(),db.scalar(select(Job)))
        assert db.get(ReplayAnalysis,queued["id"]).state=="STALE"
        assert db.scalar(select(Artifact)) is None
        assert db.get(TimelineActivePointer,"video").timeline_id=="next"


def test_unconfigured_upstream_and_missing_player_metadata_are_explicit(analysis_client):
    c,_,_=analysis_client
    r=c.post("/api/videos/video/replay-analysis",json={"timeline_version":1,"engine":"pingpong3d"},headers={"Idempotency-Key":"upstream-test-1"})
    assert r.status_code==422
    r=c.post("/api/videos/video/replay-analysis",json={"timeline_version":1,"engine":"pingpong3d",
        "players":[{"hand":"right","kit":"red"},{"hand":"left","kit":"blue"}]},headers={"Idempotency-Key":"upstream-test-1"})
    assert r.status_code==409 and r.json()["error"]["code"]=="ANALYSIS_ENGINE_UNAVAILABLE"


def test_upstream_bridge_selects_existing_rallies_and_never_provisions_cloud(tmp_path, monkeypatch):
    from spinread.product import pingpong3d_runner as runner
    repo=tmp_path/"upstream"; (repo/"scripts/match").mkdir(parents=True)
    (repo/"scripts/match/pipeline.py").touch()
    python=tmp_path/"python";python.touch();models=tmp_path/"models";models.mkdir()
    settings=Settings(_env_file=None,pingpong3d_repo=str(repo),pingpong3d_python=str(python),pingpong3d_model_dir=str(models))
    source=tmp_path/"source";source.write_bytes(b"original")
    work=tmp_path/"work";calls=[]
    monkeypatch.setenv("RUNPOD_API_KEY","never-transmit")
    monkeypatch.setenv("SPINREAD_S3_SECRET_KEY","never-transmit")
    def execute(command,**kwargs):
        calls.append((command,kwargs))
        if len(calls)==1:
            (work/"status.json").write_text(json.dumps({"status":"awaiting_selection"}))
            (work/"points.json").write_text(json.dumps({"rallies":[{"id":1,"t0":0,"t1":2},{"id":2,"t0":10,"t1":12}]}))
            return 3
        (work/"status.json").write_text(json.dumps({"status":"ready"})); return 0
    monkeypatch.setattr(runner,"_execute",execute)
    assert runner.run_match(source,work,settings,[{"start_ms":0,"end_ms":2000}],
        [{"hand":"right","kit":"red","height":180},{"hand":"left","kit":"blue"}])==work
    assert json.loads((work/"selection.json").read_text())["rallies"]==[1]
    assert json.loads((work/"match.json").read_text())["options"]["gpu"]=="local"
    assert json.loads((work/"match.json").read_text())["players"][0]["height"]==1.8
    assert all("RUNPOD_API_KEY" not in c[1]["env"] and "SPINREAD_S3_SECRET_KEY" not in c[1]["env"] for c in calls)
    assert all(c[1]["env"]["PRO_LIBRARY_DB"]==str(work/"pro_library.sqlite") for c in calls)
    assert source.read_bytes()==b"original"


def test_new_pose_does_not_hide_existing_ball_modalities():
    base=replay_evidence(None,[],2000)
    base['frames']=[[100,.2,.3]]
    base['ball_3d']={'segments':[{'frames':[[100,0,0,1],[200,1,1,1]]}]}
    base['placements']=[{'time_ms':500,'status':'CANDIDATE'}]
    fresh=replay_evidence(None,[],2000)
    fresh['pose']={'frames':[{'time_ms':100,'people':[{'world':[[0,0,1]]}]}]}
    combined=service.merge_evidence(base,fresh)
    assert combined['frames']==base['frames'] and combined['ball_3d']==base['ball_3d']
    assert combined['placements']==base['placements'] and combined['pose']==fresh['pose']
    assert 'modality_provenance' not in base
    empty=service.merge_evidence(combined,{'pose':{'frames':[{'time_ms':100,'people':[]}]}})
    assert empty['pose']==fresh['pose']


def test_empty_pose_is_not_published_as_success(analysis_client,monkeypatch):
    c,factory,settings=analysis_client
    evidence=replay_evidence(None,[],20000)
    evidence['pose']={'frames':[{'time_ms':100,'people':[]}]}
    monkeypatch.setattr(service,'_local',lambda *args:(evidence,[]))
    queued=enqueue(c)
    with factory() as db:
        service.run_replay_analysis(db,settings,Store(),db.scalar(select(Job)))
        row=db.get(ReplayAnalysis,queued['id'])
        assert row.state=='FAILED' and row.error_code=='NO_USABLE_EVIDENCE'
        assert row.artifact_id is None
