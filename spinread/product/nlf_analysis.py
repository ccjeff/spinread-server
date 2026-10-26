"""Local NLF execution and immutable, authenticated SMPL chunk publication."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time

from spinread.product.pingpong3d_runner import UpstreamUnavailable

ASSET_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
MAX_ASSET_BYTES = 16 * 1024 * 1024
_status_cache = {}


def config(settings):
    from pingpong_training.analysis.nlf import NLFConfig
    return NLFConfig(weights=settings.nlf_weights, faces=settings.nlf_faces,
                     fps=settings.nlf_fps, batch_size=settings.nlf_batch_size)


def status(settings, *, refresh=False):
    """No network, model download or GPU inference; never expose private paths."""
    key = (settings.nlf_weights, settings.nlf_faces, settings.nlf_fps, settings.nlf_batch_size)
    cached = _status_cache.get(key)
    if not refresh and cached and time.monotonic() - cached[0] < 15:
        return cached[1]
    try:
        from pingpong_training.analysis.nlf import preflight
        result = preflight(config(settings))
    except ImportError:
        result = {"configured": False, "available": False, "status": "UNAVAILABLE",
                  "missing": ["NLF_RUNTIME_MISSING"], "no_fallback": True}
    public = {k: result[k] for k in ("configured", "available", "status", "missing", "no_fallback") if k in result}
    _status_cache[key] = (time.monotonic(), public)
    return public


def checked_asset(root, item):
    root = Path(root).resolve()
    filename = item.get("file")
    if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}\.(bin|json)", filename):
        raise UpstreamUnavailable("NLF_INVALID_ASSET")
    candidate = root / filename
    path = candidate.resolve()
    if candidate.is_symlink() or not path.is_relative_to(root) or not path.is_file():
        raise UpstreamUnavailable("NLF_INVALID_ASSET")
    size = item.get("byte_length")
    if type(size) is not int or not 0 < size <= MAX_ASSET_BYTES or path.stat().st_size != size:
        raise UpstreamUnavailable("NLF_INVALID_ASSET_SIZE")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != item.get("sha256"):
        raise UpstreamUnavailable("NLF_ASSET_HASH_MISMATCH")
    return data


def publish_mesh(s3, video, analysis, manifest, root):
    """Return a storage index kept out of the public replay document."""
    mesh = manifest.get("mesh") or {}
    topology = mesh.get("topology") or {}
    ids = {topology.get("faces_asset_id"), topology.get("parts_asset_id")}
    ids.update(c.get("asset_id") for c in mesh.get("chunks", []))
    ids.discard(None)
    index = {}
    for asset_id in sorted(ids):
        if not isinstance(asset_id, str) or not ASSET_ID.fullmatch(asset_id):
            raise UpstreamUnavailable("NLF_INVALID_ASSET")
        spec = manifest.get("assets", {}).get(asset_id)
        if not isinstance(spec, dict):
            raise UpstreamUnavailable("NLF_INVALID_ASSET")
        data = checked_asset(root, spec)
        key = f"users/{video.owner_id}/videos/{video.id}/replay/{analysis.id}/mesh/{asset_id}"
        s3.put_bytes(key, data, content_type="application/octet-stream")
        index[asset_id] = {"object_key": key, "byte_length": len(data), "sha256": spec["sha256"]}
    return index


def public_mesh(mesh, video_id, version, artifact_id):
    """Only server-relative, authenticated asset URLs are emitted to the browser."""
    def url(asset_id):
        if not isinstance(asset_id, str) or not ASSET_ID.fullmatch(asset_id):
            raise UpstreamUnavailable("NLF_INVALID_ASSET")
        return (f"/api/videos/{video_id}/replay-mesh/assets/{asset_id}"
                f"?timeline_version={version}&analysis_artifact_id={artifact_id}")
    topology = dict(mesh["topology"])
    topology["faces_url"] = url(topology.pop("faces_asset_id"))
    if topology.get("parts_asset_id"):
        topology["parts_url"] = url(topology.pop("parts_asset_id"))
    chunks = []
    for chunk in mesh.get("chunks", []):
        chunk = dict(chunk)
        chunk["url"] = url(chunk.pop("asset_id"))
        chunks.append(chunk)
    return {**mesh, "topology": topology, "chunks": chunks}


def run(source, work, settings, source_sha256, duration_ms):
    """Build full-video NLF evidence; no partial result is mislabeled complete."""
    from pingpong_training.analysis.nlf import run_nlf
    from pingpong_training.analysis.replay import replay_evidence
    if not status(settings, refresh=True).get("available"):
        raise UpstreamUnavailable("NLF_UNAVAILABLE")
    manifest = run_nlf(source, work, config(settings))
    if (not manifest.get("complete") or manifest.get("source_sha256") != source_sha256
            or abs(manifest.get("duration_ms", 0) - duration_ms) > 100):
        raise UpstreamUnavailable("NLF_SOURCE_MISMATCH")
    frames = []
    header = None
    axes = {"TABLE_METERS_Z_UP": "X_TABLE_LENGTH_Y_TABLE_WIDTH_Z_UP",
            "HIP_RELATIVE_MODEL_3D": "X_RIGHT_Y_DOWN_Z_FORWARD"}
    for chunk in manifest.get("pose_chunks", []):
        spec = manifest.get("assets", {}).get(chunk["asset_id"])
        if not isinstance(spec, dict):
            raise UpstreamUnavailable("NLF_INVALID_ASSET")
        pose = json.loads(checked_asset(work, spec))
        coordinate = pose.get("coordinate_space")
        if (coordinate not in axes or pose.get("model") != "NLF" or pose.get("joint_schema") != "NLF41"
                or pose.get("unit") != "m" or pose.get("fps") != settings.nlf_fps
                or pose.get("axes", axes[coordinate]) != axes[coordinate]):
            raise UpstreamUnavailable("NLF_INVALID_POSE_COORDINATES")
        header = {k:v for k,v in pose.items() if k != "frames"}
        for frame in pose["frames"]:
            if type(frame.get("time_ms")) is not int or not 0 <= frame["time_ms"] < duration_ms:
                raise UpstreamUnavailable("NLF_INVALID_POSE_CLOCK")
            frames.append({**frame, "coordinate_space": pose["coordinate_space"],
                           "axes": axes[coordinate], "calibration_id": pose.get("calibration_id")})
    frames.sort(key=lambda f:f["time_ms"])
    if (not frames or not header or manifest.get("statistics", {}).get("frames") != len(frames)
            or abs(len(frames) - manifest["duration_ms"] * settings.nlf_fps / 1000) > 1
            or any(f["time_ms"] != round(i * 1000 / settings.nlf_fps) for i,f in enumerate(frames))):
        raise UpstreamUnavailable("NLF_INVALID_POSE_CLOCK")
    spaces = {f["coordinate_space"] for f in frames}
    coordinate = next(iter(spaces)) if len(spaces)==1 else "PER_FRAME"
    pose = {**header, "frames":frames, "coordinate_space":coordinate,
            "axes": axes[coordinate] if coordinate != "PER_FRAME" else "PER_FRAME"}
    detected = any(f["people"] for f in frames)
    evidence = {**replay_evidence(None, [], duration_ms), "pose":pose, "mesh":manifest["mesh"],
        "provenance":{"provider":"NLF_SMPL","source_sha256":source_sha256,
                      "model_sha256":manifest.get("identity",{}).get("model_sha256")},
        "capabilities":{"pose_3d":"MODEL_ESTIMATE" if detected else "UNAVAILABLE",
                        "smpl_mesh":"MODEL_ESTIMATE" if manifest["mesh"]["chunks"] else "UNAVAILABLE"},
        "limitations":manifest["mesh"].get("limitations",[])}
    return evidence, manifest
