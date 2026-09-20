#!/usr/bin/env python3
"""Generate sample/session.mp4: a 75 s synthetic training session.

Structure (same pattern as tests/conftest.py):
  0-25 s   moving content + 3 kHz click every 2 s   (rally-like)
  25-45 s  static frame + silence                     (break / pickup)
  45-75 s  moving content + clicks                    (rally-like)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
OUT = Path(__file__).resolve().parent.parent / "sample" / "session.mp4"


def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, timeout=300)
    if proc.returncode != 0:
        sys.exit(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[:500]}")


def encode_segment(out: Path, video_src: str, audio_src: str) -> None:
    run(
        [
            FFMPEG, "-y", "-v", "error",
            "-f", "lavfi", "-i", video_src,
            "-f", "lavfi", "-i", audio_src,
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-ar", "16000", "-ac", "1",
            "-shortest", str(out),
        ]
    )


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    clicks = "aevalsrc=exprs='0.6*sin(2*PI*3000*t)*lt(mod(t,2),0.02)':s=16000:d={d}"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        seg1 = work / "seg1.mp4"
        encode_segment(seg1, "testsrc2=size=640x360:rate=30:duration=25", clicks.format(d=25))
        seg2 = work / "seg2.mp4"
        encode_segment(seg2, "color=c=0x404040:size=640x360:rate=30:duration=20", "anullsrc=r=16000:cl=mono:d=20")
        seg3 = work / "seg3.mp4"
        encode_segment(seg3, "testsrc2=size=640x360:rate=30:duration=30", clicks.format(d=30))
        run(
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
                str(OUT),
            ]
        )
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
