"""End-to-end: real Postgres + MinIO + ffmpeg, in-process worker loop.

Flow: login -> POST /video-uploads -> boto3 multipart direct upload ->
complete -> drain worker queue until READY -> assert status/timeline/media.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from spinread.api.main import app
from spinread.core.db import make_engine, make_session_factory
from spinread.core.storage import S3ObjectStore
from spinread.worker.main import run_worker

pytestmark = pytest.mark.usefixtures("require_services")

READY_TIMEOUT_S = 300


@pytest.fixture(scope="module")
def client(require_services):
    with TestClient(app) as c:  # startup: bucket bootstrap, seed, embed worker
        yield c


def _login(client: TestClient) -> dict[str, str]:
    resp = client.post(
        "/api/auth/login",
        json={"email": "demo@spinread.local", "password": "spinread-demo"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_health(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_e2e_upload_to_ready(client, synthetic_training_video: Path):
    headers = _login(client)
    video_path = synthetic_training_video
    size = video_path.stat().st_size

    # --- create upload session ---------------------------------------------
    resp = client.post(
        "/api/video-uploads",
        headers=headers,
        json={
            "filename": "training_60s.mp4",
            "byte_size": size,
            "content_type": "video/mp4",
            "session_type": "TRAINING",
            "target_player": {"mode": "NEAR"},
        },
    )
    assert resp.status_code == 201, resp.text
    upload = resp.json()
    video_id = upload["video_id"]
    assert upload["part_size"] == 16 * 1024 * 1024
    assert len(upload["parts"]) >= 1

    # --- direct multipart upload (simulating the browser) -------------------
    s3 = S3ObjectStore()
    data = video_path.read_bytes()
    completed_parts = []
    with httpx.Client(timeout=120) as raw:
        for part in upload["parts"]:
            n = part["part_number"]
            chunk = data[(n - 1) * upload["part_size"] : n * upload["part_size"]]
            put = raw.put(part["presigned_url"], content=chunk)
            assert put.status_code == 200, put.text
            completed_parts.append(
                {"part_number": n, "etag": put.headers["ETag"].strip('"')}
            )

    # --- complete (synchronous finalize) -------------------------------------
    resp = client.post(
        f"/api/video-uploads/{upload['upload_id']}/complete",
        headers=headers,
        json={"parts": completed_parts},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"video_id": video_id, "state": "UPLOADED"}

    # idempotent replay
    resp2 = client.post(
        f"/api/video-uploads/{upload['upload_id']}/complete",
        headers=headers,
        json={"parts": completed_parts},
    )
    assert resp2.status_code == 200
    assert resp2.json()["state"] in ("UPLOADED", "PROBING", "NORMALIZING")

    # --- drain the queue with the real worker loop ---------------------------
    deadline = time.time() + READY_TIMEOUT_S
    state = ""
    while time.time() < deadline:
        run_worker(once=True, worker_id="e2e-test")
        resp = client.get(f"/api/videos/{video_id}/processing-status", headers=headers)
        assert resp.status_code == 200, resp.text
        state = resp.json()["state"]
        if state in ("READY", "PARTIAL_READY", "PERMANENT_FAILURE", "RETRYABLE_FAILURE"):
            break
        time.sleep(1)
    assert state == "READY", f"final state {state}: {resp.json()}"

    status = resp.json()
    assert status["progress_pct"] == 100
    by_stage = {s["stage"]: s for s in status["stages"]}
    assert by_stage["PROBE"]["status"] == "SUCCEEDED"
    assert by_stage["TIMELINE"]["status"] == "SUCCEEDED"

    # --- timeline --------------------------------------------------------------
    resp = client.get(f"/api/videos/{video_id}/timelines/active", headers=headers)
    assert resp.status_code == 200, resp.text
    timeline = resp.json()
    assert timeline["version"] == 1
    rally_items = [i for i in timeline["items"] if i["type"] == "RALLY_LIKE"]
    assert len(rally_items) >= 1, timeline["items"]
    for item in timeline["items"]:
        assert item["end_ms"] > item["start_ms"]
        assert item["attributes"].get("poc_item_id")

    # --- media gateway -----------------------------------------------------------
    resp = client.get(f"/api/videos/{video_id}/stream/master.m3u8", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    seg_line = next(
        l for l in resp.text.splitlines() if l and not l.startswith("#")
    )
    assert seg_line.startswith(f"/api/videos/{video_id}/stream/")
    seg_name = seg_line.rsplit("/", 1)[1]
    seg = client.get(f"/api/videos/{video_id}/stream/{seg_name}", headers=headers)
    assert seg.status_code == 200
    assert len(seg.content) > 0

    resp = client.get(
        f"/api/videos/{video_id}/media/proxy",
        headers={**headers, "Range": "bytes=0-1023"},
    )
    assert resp.status_code == 206
    assert len(resp.content) == 1024
    assert resp.headers["Content-Range"].startswith("bytes 0-1023/")

    thumb = client.get(f"/api/videos/{video_id}/thumbs/0.jpg", headers=headers)
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/jpeg"

    # --- listing + ownership ---------------------------------------------------
    videos = client.get("/api/videos", headers=headers)
    assert any(v["id"] == video_id for v in videos.json())


def test_auth_required(client):
    assert client.get("/api/videos").status_code == 401
