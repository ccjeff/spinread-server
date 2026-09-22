"""Test fixtures: synthetic 60 s training video via ffmpeg (POC pattern),
plus live Postgres/MinIO handles from the docker-compose dev stack.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

os.environ.setdefault("SPINREAD_DB_URL", "postgresql+psycopg://spinread:spinread@localhost:5432/spinread")
os.environ.setdefault("SPINREAD_S3_ENDPOINT", "http://localhost:9000")


def _run_ffmpeg(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, timeout=180)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg fixture command failed: {proc.stderr.decode(errors='replace')[:500]}"
        )


def _encode_segment(out: Path, video_src: str, audio_src: str) -> None:
    _run_ffmpeg(
        [
            FFMPEG, "-y", "-v", "error",
            "-f", "lavfi", "-i", video_src,
            "-f", "lavfi", "-i", audio_src,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "16000", "-ac", "1",
            "-shortest", str(out),
        ]
    )


@pytest.fixture(scope="session")
def synthetic_training_video(tmp_path_factory) -> Path:
    """60 s: motion+clicks (0-20 s) / static+silence (20-35 s) / motion+clicks (35-60 s)."""
    work = tmp_path_factory.mktemp("training")
    clicks = "aevalsrc=exprs='0.6*sin(2*PI*3000*t)*lt(mod(t,2),0.02)':s=16000:d={d}"
    seg1 = work / "seg1.mp4"
    _encode_segment(seg1, "testsrc2=size=320x180:rate=30:duration=20", clicks.format(d=20))
    seg2 = work / "seg2.mp4"
    _encode_segment(seg2, "color=c=0x404040:size=320x180:rate=30:duration=15", "anullsrc=r=16000:cl=mono:d=15")
    seg3 = work / "seg3.mp4"
    _encode_segment(seg3, "testsrc2=size=320x180:rate=30:duration=25", clicks.format(d=25))
    out = work / "training_60s.mp4"
    _run_ffmpeg(
        [
            FFMPEG, "-y", "-v", "error",
            "-i", str(seg1), "-i", str(seg2), "-i", str(seg3),
            "-filter_complex",
            "[0:v]fps=30,format=yuv420p[v0];[1:v]fps=30,format=yuv420p[v1];"
            "[2:v]fps=30,format=yuv420p[v2];"
            "[0:a]aformat=sample_rates=16000:channel_layouts=mono[a0];"
            "[1:a]aformat=sample_rates=16000:channel_layouts=mono[a1];"
            "[2:a]aformat=sample_rates=16000:channel_layouts=mono[a2];"
            "[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[v][a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "16000", "-ac", "1",
            str(out),
        ]
    )
    return out


@pytest.fixture(scope="session")
def require_services():
    """Skip when the compose stack is not up."""
    import socket

    for port in (5432, 9000):
        try:
            socket.create_connection(("localhost", port), timeout=2).close()
        except OSError:
            pytest.skip(f"localhost:{port} not reachable; run docker compose up -d")


# --- shared live-stack helpers -------------------------------------------------

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

READY_TIMEOUT_S = 300


@pytest.fixture(scope="session")
def client(require_services):
    """Session-wide TestClient (startup bootstraps bucket, seeds user, embed worker)."""
    from spinread.api.main import app

    with TestClient(app) as c:
        yield c


def login_headers(client: TestClient) -> dict[str, str]:
    resp = client.post(
        "/api/auth/login",
        json={"email": "demo@spinread.local", "password": "spinread-demo"},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def upload_video(client: TestClient, headers: dict, video_path: Path) -> str:
    """Create upload session, direct multipart upload, complete. Returns video_id."""
    size = video_path.stat().st_size
    resp = client.post(
        "/api/video-uploads",
        headers=headers,
        json={
            "filename": video_path.name,
            "byte_size": size,
            "content_type": "video/mp4",
            "session_type": "TRAINING",
            "target_player": {"mode": "NEAR"},
        },
    )
    assert resp.status_code == 201, resp.text
    upload = resp.json()
    video_id = upload["video_id"]

    data = video_path.read_bytes()
    completed = []
    with httpx.Client(timeout=120) as raw:
        for part in upload["parts"]:
            n = part["part_number"]
            chunk = data[(n - 1) * upload["part_size"] : n * upload["part_size"]]
            put = raw.put(part["presigned_url"], content=chunk)
            assert put.status_code == 200, put.text
            completed.append({"part_number": n, "etag": put.headers["ETag"].strip('"')})

    resp = client.post(
        f"/api/video-uploads/{upload['upload_id']}/complete",
        headers=headers,
        json={"parts": completed},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "UPLOADED"
    return video_id


def wait_until_ready(
    client: TestClient, headers: dict, video_id: str, timeout_s: int = READY_TIMEOUT_S
) -> dict:
    """Drain the queue with the real worker loop until terminal state."""
    import time

    from spinread.worker.main import run_worker

    deadline = time.time() + timeout_s
    status = {}
    while time.time() < deadline:
        run_worker(once=True, worker_id="pytest-drain")
        resp = client.get(f"/api/videos/{video_id}/processing-status", headers=headers)
        assert resp.status_code == 200, resp.text
        status = resp.json()
        if status["state"] in (
            "READY", "PARTIAL_READY", "PERMANENT_FAILURE", "RETRYABLE_FAILURE"
        ):
            return status
        time.sleep(0.5)
    raise AssertionError(f"pipeline did not reach a terminal state: {status}")


@pytest.fixture(scope="session")
def ready_video(client, synthetic_training_video) -> tuple[str, dict[str, str]]:
    """One uploaded 60 s synthetic video driven to READY; shared across modules."""
    headers = login_headers(client)
    video_id = upload_video(client, headers, synthetic_training_video)
    status = wait_until_ready(client, headers, video_id)
    assert status["state"] == "READY", status
    return video_id, headers
