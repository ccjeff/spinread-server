#!/usr/bin/env python3
"""Upload a video through the real API and wait for the pipeline.

Usage: python scripts/upload_video.py sample/session.mp4 [--wait] [--base http://localhost:8000]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx

TERMINAL = {"READY", "PARTIAL_READY", "PERMANENT_FAILURE", "RETRYABLE_FAILURE"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("--wait", action="store_true", help="poll processing-status until terminal")
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--email", default="demo@spinread.local")
    parser.add_argument("--password", default="spinread-demo")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    video_path: Path = args.video
    if not video_path.exists():
        sys.exit(f"no such file: {video_path}")

    client = httpx.Client(base_url=args.base, timeout=120)
    login = client.post(
        "/api/auth/login", json={"email": args.email, "password": args.password}
    )
    login.raise_for_status()
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}

    size = video_path.stat().st_size
    resp = client.post(
        "/api/video-uploads",
        headers=h,
        json={
            "filename": video_path.name,
            "byte_size": size,
            "content_type": "video/mp4",
            "session_type": "TRAINING",
            "target_player": {"mode": "NEAR"},
        },
    )
    resp.raise_for_status()
    up = resp.json()
    print(f"video_id={up['video_id']} upload_id={up['upload_id']} parts={len(up['parts'])}")

    data = video_path.read_bytes()
    completed = []
    with httpx.Client(timeout=600) as raw:
        for part in up["parts"]:
            n = part["part_number"]
            chunk = data[(n - 1) * up["part_size"]: n * up["part_size"]]
            put = raw.put(part["presigned_url"], content=chunk)
            put.raise_for_status()
            completed.append({"part_number": n, "etag": put.headers["ETag"].strip('"')})
    resp = client.post(
        f"/api/video-uploads/{up['upload_id']}/complete",
        headers=h,
        json={"parts": completed},
    )
    resp.raise_for_status()
    print("complete:", resp.json())

    if not args.wait:
        return
    deadline = time.time() + args.timeout
    vid = up["video_id"]
    while time.time() < deadline:
        st = client.get(f"/api/videos/{vid}/processing-status", headers=h).json()
        print(f"  state={st['state']} progress={st['progress_pct']}%", flush=True)
        if st["state"] in TERMINAL:
            print("final:", st["state"])
            sys.exit(0 if st["state"] in ("READY", "PARTIAL_READY") else 1)
        time.sleep(3)
    sys.exit("timeout waiting for pipeline")


if __name__ == "__main__":
    main()
