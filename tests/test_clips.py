"""Clip / highlight exports: idempotency, rendering, download (real stack)."""

from __future__ import annotations

import subprocess
import time

import pytest

from spinread.worker.main import run_worker

pytestmark = pytest.mark.usefixtures("require_services")

PRE_ROLL_MS = 800
POST_ROLL_MS = 1200


def _drain_clip_jobs(client, headers, video_id, clip_id, timeout_s=120):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        run_worker(once=True, worker_id="pytest-clips")
        resp = client.get(f"/api/clips/{clip_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        if resp.json()["status"] != "RENDERING":
            return resp.json()
        time.sleep(0.5)
    raise AssertionError("clip render did not finish")


def _probe_duration_ms(path) -> int:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", str(path),
        ],
        capture_output=True,
        timeout=60,
    )
    import json

    return int(round(float(json.loads(proc.stdout)["format"]["duration"]) * 1000))


def test_clip_render_download_and_idempotency(client, ready_video, tmp_path):
    video_id, headers = ready_video
    items = client.get(
        f"/api/videos/{video_id}/timelines/active", headers=headers
    ).json()["items"]
    rally = next(i for i in items if i["type"] == "RALLY")

    resp = client.post(
        "/api/clips",
        headers=headers,
        json={"video_id": video_id, "timeline_item_id": rally["item_id"]},
    )
    assert resp.status_code == 201, resp.text
    clip = resp.json()
    assert clip["status"] == "RENDERING"
    assert clip["download_url"] is None

    final = _drain_clip_jobs(client, headers, video_id, clip["clip_id"])
    assert final["status"] == "READY"
    assert final["download_url"] == f"/api/clips/{clip['clip_id']}/download"

    dl = client.get(final["download_url"], headers=headers)
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "video/mp4"
    out = tmp_path / "clip.mp4"
    out.write_bytes(dl.content)
    duration = _probe_duration_ms(out)
    expected = (rally["end_ms"] - rally["start_ms"]) + PRE_ROLL_MS + POST_ROLL_MS
    assert abs(duration - expected) < 800, (duration, expected)

    # idempotent replay: same params -> same clip_id
    resp2 = client.post(
        "/api/clips",
        headers=headers,
        json={"video_id": video_id, "timeline_item_id": rally["item_id"]},
    )
    assert resp2.status_code == 201
    assert resp2.json()["clip_id"] == clip["clip_id"]
    assert resp2.json()["status"] == "READY"

    # list endpoint shows it
    lst = client.get("/api/clips", headers=headers, params={"video_id": video_id})
    assert any(c["clip_id"] == clip["clip_id"] for c in lst.json())


def test_highlight_reel_concat(client, ready_video, tmp_path):
    video_id, headers = ready_video
    items = client.get(
        f"/api/videos/{video_id}/timelines/active", headers=headers
    ).json()["items"]
    rallies = sorted((i for i in items if i["type"] == "RALLY"), key=lambda i: i["start_ms"])
    assert len(rallies) == 2

    resp = client.post(
        "/api/highlight-reels",
        headers=headers,
        json={
            "video_id": video_id,
            "timeline_item_ids": [r["item_id"] for r in rallies],
        },
    )
    assert resp.status_code == 201, resp.text
    reel = resp.json()

    deadline = time.time() + 180
    while time.time() < deadline:
        run_worker(once=True, worker_id="pytest-highlight")
        st = client.get(f"/api/highlight-reels/{reel['clip_id']}", headers=headers).json()
        if st["status"] != "RENDERING":
            break
        time.sleep(0.5)
    assert st["status"] == "READY", st

    dl = client.get(f"/api/highlight-reels/{reel['clip_id']}/download", headers=headers)
    assert dl.status_code == 200
    out = tmp_path / "highlight.mp4"
    out.write_bytes(dl.content)
    duration = _probe_duration_ms(out)
    video_ms = client.get(f"/api/videos/{video_id}", headers=headers).json()["duration_ms"]
    expected = sum(
        min(r["end_ms"] + POST_ROLL_MS, video_ms) - max(r["start_ms"] - PRE_ROLL_MS, 0)
        for r in rallies
    )
    assert abs(duration - expected) < 1200, (duration, expected)
