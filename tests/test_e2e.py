"""End-to-end: real Postgres + MinIO + ffmpeg, in-process worker loop.

Flow: login -> POST /video-uploads -> multipart direct upload -> complete ->
drain worker queue until READY -> assert 9-stage DAG, timeline, media gateway.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import login_headers, upload_video, wait_until_ready

pytestmark = pytest.mark.usefixtures("require_services")


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_e2e_upload_to_ready(client, synthetic_training_video: Path):
    headers = login_headers(client)
    video_id = upload_video(client, headers, synthetic_training_video)

    status = wait_until_ready(client, headers, video_id)
    assert status["state"] == "READY", status
    assert status["progress_pct"] == 100
    by_stage = {s["stage"]: s for s in status["stages"]}
    for stage in (
        "PROBE", "NORMALIZE", "QUALITY", "ACTIVITY",
        "RALLY", "EVENTS", "TIMELINE", "METRICS", "REPORT",
    ):
        assert by_stage[stage]["status"] == "SUCCEEDED", by_stage

    # --- timeline hierarchy ---------------------------------------------------
    resp = client.get(f"/api/videos/{video_id}/timelines/active", headers=headers)
    assert resp.status_code == 200, resp.text
    timeline = resp.json()
    assert timeline["version"] >= 1
    items = timeline["items"]
    by_id = {i["item_id"]: i for i in items}
    top = [i for i in items if i["attributes"].get("poc_item_id")]
    rally_like = [i for i in top if i["type"] == "RALLY_LIKE"]
    assert len(rally_like) >= 2, items
    rallies = [i for i in items if i["type"] == "RALLY"]
    assert rallies, items
    hits = [i for i in items if i["type"] == "HIT_CANDIDATE"]
    assert hits, items
    for item in items:
        assert item["end_ms"] > item["start_ms"]

    # --- media gateway -----------------------------------------------------------
    resp = client.get(f"/api/videos/{video_id}/stream/master.m3u8", headers=headers)
    assert resp.status_code == 200
    seg_line = next(l for l in resp.text.splitlines() if l and not l.startswith("#"))
    assert seg_line.startswith(f"/api/videos/{video_id}/stream/")
    seg = client.get(seg_line, headers=headers)
    assert seg.status_code == 200

    resp = client.get(
        f"/api/videos/{video_id}/media/proxy",
        headers={**headers, "Range": "bytes=0-1023"},
    )
    assert resp.status_code == 206
    assert len(resp.content) == 1024

    thumb = client.get(f"/api/videos/{video_id}/thumbs/0.jpg", headers=headers)
    assert thumb.status_code == 200

    videos = client.get("/api/videos", headers=headers)
    assert any(v["id"] == video_id for v in videos.json())


def test_auth_required(client):
    assert client.get("/api/videos").status_code == 401


def test_manual_rerun(client, ready_video):
    """POST pipeline-runs: full rerun succeeds again (media_version bumps), 409 while running."""
    import time

    from spinread.worker.main import run_worker

    video_id, headers = ready_video
    resp = client.post(f"/api/videos/{video_id}/pipeline-runs", headers=headers)
    assert resp.status_code == 201, resp.text
    conflict = client.post(f"/api/videos/{video_id}/pipeline-runs", headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "PIPELINE_RUN_ACTIVE"

    deadline = time.time() + 300
    status = {}
    while time.time() < deadline:
        run_worker(once=True, worker_id="pytest-rerun")
        status = client.get(
            f"/api/videos/{video_id}/processing-status", headers=headers
        ).json()
        if status["state"] in ("READY", "PARTIAL_READY", "PERMANENT_FAILURE", "RETRYABLE_FAILURE"):
            break
        time.sleep(0.5)
    assert status["state"] == "READY", status
    assert all(s["status"] == "SUCCEEDED" for s in status["stages"]), status["stages"]

    # timeline version advanced; media still served (new ACTIVE derived assets)
    tl = client.get(f"/api/videos/{video_id}/timelines/active", headers=headers).json()
    assert tl["version"] >= 2
    proxy = client.get(
        f"/api/videos/{video_id}/media/proxy",
        headers={**headers, "Range": "bytes=0-1023"},
    )
    assert proxy.status_code == 206
