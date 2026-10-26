"""Mesh ownership and independent enrichment never erase the reviewed record."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from test_replay_analysis import analysis_client, Store
from test_quizzes import quiz_client
from spinread.api.main import app
from spinread.api.deps import get_s3
from spinread.api.routers.replay import review_data
from spinread.core.models import Artifact, ReplayAnalysis, ReplayReview, Timeline, TimelineActivePointer, Video
from spinread.product import replay_analysis as service
from spinread.product.nlf_analysis import checked_asset, public_mesh, publish_mesh
from spinread.product.pingpong3d_runner import UpstreamUnavailable
from pingpong_training.analysis.replay import replay_evidence


def publish(db, store, evidence, **kwargs):
    row = ReplayAnalysis(video_id="video", timeline_id="timeline", timeline_version=1, engine="local", state="RUNNING")
    db.add(row); db.flush()
    service.publish_analysis(db, store, row, db.get(Video, "video"), evidence, **kwargs)
    row.state = "READY"; db.flush()
    return row


def test_independent_3d_enrichment_retains_review_without_writing_it(analysis_client):
    _, factory, _ = analysis_client
    store = Store()
    base = replay_evidence(None, [], 20000); base["frames"] = [[100, .2, .3]]
    with factory.begin() as db:
        first = publish(db, store, base)
        data = {"analysis_artifact_id": first.artifact_id, "reviews": [
            {"rally_id": "rally", "time_ms": 266, "phase": "SERVE", "status": "CONFIRMED"}]}
        review = ReplayReview(video_id="video", reviewer_id="owner", timeline_version=1, version=3, data=data)
        db.add(review); db.flush()
        before = copy.deepcopy(review.data)
        second = publish(db, store, {"pose": {"frames": [{"time_ms": 100, "people": [{"world": [[0, 0, 1]]}]}]}})
        third = publish(db, store, {"ball_3d": {"segments": [{"frames": [[100, 0, 0, 1], [200, 1, 1, 1]]}]}})
        result = review_data(db, db.get(Video, "video"), 1)
        assert not result["stale"] and result["reviews"] == before["reviews"]
        assert result["analysis_artifact_id"] == third.artifact_id
        assert review.data == before and review.version == 3
        assert first.artifact_id in db.get(Artifact, third.artifact_id).metrics["placement_review_ancestor_ids"]
        changed = publish(db, store, {"frames": [[100, .8, .3]]})
        assert "placement_review_ancestor_ids" not in db.get(Artifact, changed.artifact_id).metrics
        assert review_data(db, db.get(Video, "video"), 1)["stale"]
        assert review.data == before and review.version == 3


def test_asset_read_is_scoped_to_video_timeline_and_allowlist(analysis_client):
    c, factory, _ = analysis_client
    store = Store(); data = b"\0\0\1\0\2\0"
    with factory.begin() as db:
        video = db.get(Video, "video")
        key = f"users/{video.owner_id}/videos/video/replay/test/mesh/faces"
        store.objects[key] = data
        spec = {"object_key": key, "byte_length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        row = publish(db, store, replay_evidence(None, [], 20000), mesh_assets={"faces": spec})
        aid = row.artifact_id
    app.dependency_overrides[get_s3] = lambda: store
    path = f"/api/videos/video/replay-mesh/assets/faces?timeline_version=1&analysis_artifact_id={aid}"
    response = c.get(path)
    assert response.status_code == 200 and response.content == data
    assert response.headers["cache-control"] == "private, no-store"
    assert c.get(path, headers={"X-Test-User": "other"}).status_code in (403, 404)
    assert c.get(path.replace("assets/faces", "assets/unknown")).status_code == 404
    assert c.get(path.replace(aid, "unrelated")).status_code == 404
    assert c.get(path.replace("version=1", "version=2")).status_code == 409
    store.objects[key] = b"corrupt"
    assert c.get(path).status_code == 404
    with factory.begin() as db:
        db.add(Timeline(id="next", video_id="video", version=2, state="PUBLISHED")); db.flush()
        db.get(TimelineActivePointer, "video").timeline_id = "next"
    assert c.get(path).status_code == 409


def test_mesh_publication_validates_paths_hashes_and_emits_only_auth_urls(analysis_client, tmp_path):
    _, factory, _ = analysis_client
    root = tmp_path / "mesh"; root.mkdir()
    data = b"\0\0\1\0\2\0"; (root / "faces.bin").write_bytes(data)
    spec = {"file": "faces.bin", "byte_length": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    mesh = {"provider": "NLF_SMPL", "topology": {"faces_asset_id": "faces", "index_type": "uint16"}, "chunks": []}
    manifest = {"mesh": mesh, "assets": {"faces": spec}}
    store = Store()
    with factory.begin() as db:
        row = ReplayAnalysis(video_id="video", timeline_id="timeline", timeline_version=1, engine="nlf", state="RUNNING")
        db.add(row); db.flush()
        index = publish_mesh(store, db.get(Video, "video"), row, manifest, root)
        assert index["faces"]["byte_length"] == len(data)
    view = public_mesh(mesh, "video", 1, "artifact")
    assert view["topology"]["faces_url"] == "/api/videos/video/replay-mesh/assets/faces?timeline_version=1&analysis_artifact_id=artifact"
    assert "faces_asset_id" not in view["topology"] and mesh["topology"]["faces_asset_id"] == "faces"
    (tmp_path / "secret.bin").write_bytes(data)
    with pytest.raises(UpstreamUnavailable):
        checked_asset(root, {**spec, "file": "../secret.bin"})
    with pytest.raises(UpstreamUnavailable):
        checked_asset(root, {**spec, "sha256": "incorrect"})


def test_nlf_missing_runtime_is_explicit_without_queueing_or_erasing(analysis_client):
    c, factory, _ = analysis_client
    response = c.get("/api/videos/video/replay-analysis?timeline_version=1")
    assert response.status_code == 200
    state = response.json()
    assert not state["nlf"]["available"] and "nlf" not in state["available_engines"]
    assert "NLF_WEIGHTS_MISSING" in state["nlf"]["missing"]
    response = c.post("/api/videos/video/replay-analysis", json={"timeline_version":1,"engine":"nlf"},
                      headers={"Idempotency-Key":"missing-nlf-test"})
    assert response.status_code == 409
    with factory() as db:
        assert db.get(TimelineActivePointer,"video").timeline_id == "timeline"


def test_nlf_job_publishes_actual_producer_assets_before_scratch_cleanup(analysis_client, monkeypatch):
    """Isolated producer fixture tests transport, never published as user evidence."""
    from sqlalchemy import select
    from spinread.core.models import Job
    from spinread.product import nlf_analysis
    from pingpong_training.analysis import nlf
    c, factory, settings = analysis_client
    monkeypatch.setattr(nlf_analysis, "status", lambda *args, **kwargs:{"available":True,"status":"READY","missing":[]})
    faces = b"\0\0\1\0\2\0"
    vertices = b"\0" * (6890 * 3 * 2)
    def producer(source, work, config):
        work.mkdir(parents=True)
        assets = {}
        for aid, data in (("faces",faces),("vertices",vertices)):
            (work / (aid + ".bin")).write_bytes(data)
            assets[aid] = {"file":aid+".bin","byte_length":len(data),"sha256":hashlib.sha256(data).hexdigest()}
        pose = {"model":"NLF","joint_schema":"NLF41","unit":"m","fps":12,"bones":[],
            "coordinate_space":"HIP_RELATIVE_MODEL_3D","axes":"X_RIGHT_Y_DOWN_Z_FORWARD",
            "frames":[{"time_ms":round(i*1000/12),"people":[{
                "player_id":"track-0","world":[[0,0,0]]*41,"image":[[.5,.5]]*41}] if i==0 else []} for i in range(240)]}
        raw = json.dumps(pose).encode(); (work / "pose.json").write_bytes(raw)
        assets["pose"] = {"file":"pose.json","byte_length":len(raw),"sha256":hashlib.sha256(raw).hexdigest()}
        return {"complete":True,"source_sha256":"old-source-hash","duration_ms":20000,"assets":assets,
            "statistics":{"frames":240,"people_frames":1},
            "pose_chunks":[{"asset_id":"pose"}],"mesh":{"version":1,"provider":"NLF_SMPL","unit":"m",
                "coordinate_space":"HIP_RELATIVE_MODEL_3D","vertex_count":6890,"fps":12,
                "topology":{"faces_asset_id":"faces","index_type":"uint16"},
                "chunks":[{"id":"vertices","asset_id":"vertices","player_id":"track-0","start_ms":0,"end_ms":84,
                    "times_ms":[0],"valid":[True],"encoding":"int16-mm","byte_length":len(vertices)}]}}
    monkeypatch.setattr(nlf,"run_nlf",producer)
    response = c.post("/api/videos/video/replay-analysis",json={"timeline_version":1,"engine":"nlf"},
                      headers={"Idempotency-Key":"real-transport-fixture"})
    assert response.status_code == 202, response.text
    store = Store(); app.dependency_overrides[get_s3] = lambda:store
    with factory() as db:
        service.run_replay_analysis(db,settings,store,db.scalar(select(Job)))
        row = db.get(ReplayAnalysis,response.json()["id"])
        assert row.state == "READY", row.error_code
        evidence = service.analysis_evidence(db,store,db.get(Video,"video"),"timeline",{})
        url = evidence["mesh"]["chunks"][0]["url"]
        assert "object_key" not in json.dumps(evidence) and "file" not in evidence["mesh"]["chunks"][0]
        assert not (Path(settings.tmp_dir)/"replay-analysis"/row.id).exists()
    assert c.get(url).content == vertices
    assert c.get(url,headers={"X-Test-User":"other"}).status_code in (403,404)
    assert store.objects["original"] == b"unchanged"


def test_ball_geometry_disappears_when_its_calibration_is_edited():
    from spinread.api.routers.replay import calibrated_ball_evidence
    calibration = {"id":"camera","corners":[[0,0],[1,0],[1,1],[0,1]],"start_ms":0,"end_ms":2000}
    digest = hashlib.sha256(json.dumps(calibration,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    evidence = {"ball_3d":{"provider":"SPINREAD_BALLISTIC_EXPERIMENTAL",
        "cameras":[{"calibration_id":"camera","calibration_sha256":digest}],
        "segments":[{"start_ms":100,"end_ms":200,"calibration_id":"camera"}],"gaps":[]}}
    review = {"calibrations":[calibration]}
    assert calibrated_ball_evidence(evidence,review,2000) is evidence
    changed = {"calibrations":[{**calibration,"end_ms":1999}]}
    result = calibrated_ball_evidence(evidence,changed,2000)["ball_3d"]
    assert result["calibration_stale"] and result["segments"] == []
    assert result["gaps"][0]["end_ms"] == 2000
    assert len(evidence["ball_3d"]["segments"]) == 1
