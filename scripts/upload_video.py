"""Upload a video through the full SpinRead API flow (multipart presigned parts).

Usage:
    python scripts/upload_video.py /path/to/video.mov \
        [--api http://localhost:8000] [--email demo@spinread.local] \
        [--password spinread-demo] [--session-type TRAINING|MATCH] \
        [--target NEAR|FAR|LEFT|RIGHT] [--wait]

--wait polls processing-status until READY / PARTIAL_READY / failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path


def _req(method: str, url: str, token: str | None = None, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:  # noqa: S310 - dev tool, configurable URL
        return json.loads(resp.read())


def _put_part(url: str, data: bytes) -> str:
    req = urllib.request.Request(url, data=data, method="PUT")
    with urllib.request.urlopen(req) as resp:  # noqa: S310
        etag = resp.headers.get("ETag")
    if not etag:
        raise RuntimeError("part upload succeeded but no ETag in response")
    return etag


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--email", default="demo@spinread.local")
    ap.add_argument("--password", default="spinread-demo")
    ap.add_argument("--session-type", default="TRAINING", choices=["TRAINING", "MATCH"])
    ap.add_argument("--target", default="NEAR", choices=["NEAR", "FAR", "LEFT", "RIGHT"])
    ap.add_argument("--wait", action="store_true")
    args = ap.parse_args()

    path = Path(args.video)
    size = path.stat().st_size
    print(f"[upload] {path.name}: {size / 1e6:.1f} MB")

    token = _req("POST", f"{args.api}/api/auth/login",
                 body={"email": args.email, "password": args.password})["access_token"]
    session = _req("POST", f"{args.api}/api/video-uploads", token, {
        "filename": path.name,
        "byte_size": size,
        "content_type": "video/quicktime" if path.suffix.lower() == ".mov" else "video/mp4",
        "session_type": args.session_type,
        "target_player": {"mode": args.target},
    })
    parts_spec = session["parts"]
    part_size = session["part_size"]
    print(f"[upload] session {session['upload_id']}: {len(parts_spec)} parts "
          f"x {part_size // 2**20} MiB")

    t0 = time.time()
    done_parts = []
    with path.open("rb") as f:
        for p in parts_spec:
            n = p["part_number"]
            f.seek((n - 1) * part_size)
            etag = _put_part(p["presigned_url"], f.read(part_size))
            done_parts.append({"part_number": n, "etag": etag})
            pct = n / len(parts_spec) * 100
            rate = n * part_size / max(time.time() - t0, 1e-9) / 1e6
            print(f"[upload] part {n}/{len(parts_spec)} ({pct:.0f}%, {rate:.0f} MB/s)",
                  flush=True)

    res = _req("POST", f"{args.api}/api/video-uploads/{session['upload_id']}/complete",
               token, {"parts": done_parts})
    video_id = res["video_id"]
    print(f"[upload] complete -> video_id={video_id} state={res['state']}")

    if args.wait:
        while True:
            st = _req("GET", f"{args.api}/api/videos/{video_id}/processing-status", token)
            print(f"[pipeline] {st['state']} {st['progress_pct']}%", flush=True)
            if st["state"] in ("READY", "PARTIAL_READY") or "FAILURE" in st["state"]:
                for s in st["stages"]:
                    print(f"  {s['stage']:<10} {s['status']} attempt={s['attempt']}")
                for lim in st["limitations"]:
                    print(f"  limitation: {lim}")
                return 0 if "FAILURE" not in st["state"] else 1
            time.sleep(20)
    return 0


if __name__ == "__main__":
    sys.exit(main())
