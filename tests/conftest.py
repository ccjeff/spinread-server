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
