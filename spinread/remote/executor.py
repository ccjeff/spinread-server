"""Compute-only worker. Receives one signed source and one signed result PUT.

The SQLite stage sandbox contains only this video's media metadata, never user,
auth, quiz, coaching or training records. Its changes are returned as typed data;
it cannot connect to the application's database or mutate the source bucket.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import shutil
import tempfile

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from spinread.config import Settings
from spinread.core.models import Artifact, Base, MediaAsset, PipelineRun, StageRun, Video
from spinread.remote.protocol import COMPUTE_STAGES, RemoteError, canonical, digest, fetch, pack

ASSET_FIELDS = ("id", "video_id", "class_", "media_version", "object_key", "content_hash", "byte_size", "status", "manifest")
VIDEO_FIELDS = ("id", "owner_id", "filename", "duration_ms", "probe")
COMPUTE_SETTINGS = ("pose_fps", "blurball_batch_size", "nlf_fps", "nlf_batch_size", "pingpong3d_max_rallies")


def fields(row, names):
    return {name: getattr(row, name) for name in names}


class ScratchStore:
    def __init__(self, root, inputs):
        self.root, self.inputs, self.outputs = Path(root), inputs, {}
        self.root.mkdir(parents=True, exist_ok=True)

    def download_file(self, key, dest):
        shutil.copyfile(self.inputs[key], dest)

    def get_bytes(self, key):
        return Path(self.inputs[key]).read_bytes()

    def upload_file(self, source, key, content_type=""):
        target = self.root / f"output-{len(self.outputs)}"
        shutil.copyfile(source, target)
        self.outputs[key] = target


def compute_stage(payload, source, root, settings):
    from spinread.pipeline.stage import StageContext
    from spinread.pipeline.stages import REGISTRY
    stage = payload["stage"]
    if stage not in COMPUTE_STAGES or payload["stage_version"] != REGISTRY[stage].stage_version:
        raise RemoteError("STAGE_VERSION_MISMATCH")
    engine = create_engine("sqlite://")
    # Deliberately no external DB URL, no credentials, and no training tables.
    for model in (Video, MediaAsset, PipelineRun, StageRun, Artifact):
        model.__table__.create(engine)
    inputs = {payload["source"]["key"]: source}
    prior = {}
    for name, spec in payload["prior"].items():
        path = root / f"prior-{len(prior)}.json"
        path.write_bytes(canonical(spec["payload"]))
        inputs[spec["object_key"]] = path
        prior[name] = Artifact(**{k: v for k, v in spec.items() if k != "payload"})
    store = ScratchStore(root / "outputs", inputs)
    try:
        with Session(engine, expire_on_commit=False) as db:
            video = Video(**payload["video"])
            run = PipelineRun(**payload["run"])
            stage_run = StageRun(**payload["stage_run"])
            assets = [MediaAsset(**a) for a in payload["assets"]]
            db.add_all([video, run, stage_run, *assets]); db.flush()
            original_ids = {a.id for a in assets}
            work = root / "stage"
            work.mkdir()
            result = REGISTRY[stage].run(StageContext(db, settings, store, video, run, stage_run, work, prior))
            db.flush()
            new_assets = [fields(a, ASSET_FIELDS) for a in db.scalars(select(MediaAsset)) if a.id not in original_ids]
            return {"stage_result": asdict(result), "video": {"probe": video.probe, "duration_ms": video.duration_ms},
                    "assets": new_assets}, store.outputs
    finally:
        engine.dispose()


def compute_replay(payload, source, root, settings):
    from spinread.product.replay_analysis import _local, has_pose
    engine = payload["engine"]
    manifest, files, failures = None, {}, []
    if engine == "nlf":
        from spinread.product.nlf_analysis import run, checked_asset
        evidence, manifest = run(source, root / "nlf", settings, payload["source"]["sha256"], payload["duration_ms"])
        if not has_pose(evidence):
            failures.append("POSE_NO_DETECTIONS")
        for key, spec in manifest.get("assets", {}).items():
            checked_asset(root / "nlf", spec)
            files["mesh/" + spec["file"]] = root / "nlf" / spec["file"]
    elif engine == "pingpong3d":
        from spinread.product.pingpong3d_runner import run_match
        from pingpong_training.analysis.pingpong3d import load_pingpong3d
        directory = run_match(source, root / "match", settings, payload["rallies"], payload["players"])
        evidence = load_pingpong3d(directory, payload["duration_ms"])
        if not has_pose(evidence):
            failures.append("POSE_UNAVAILABLE")
        if not evidence.get("placements"):
            failures.append("PLACEMENTS_UNAVAILABLE")
        if not (evidence.get("ball_3d") or {}).get("segments"):
            failures.append("BALL_3D_UNAVAILABLE")
    elif engine == "local":
        evidence, failures = _local(source, root, settings, payload["duration_ms"])
    else:
        raise RemoteError("UNSUPPORTED_REPLAY_ENGINE")
    evidence["analysis_warnings"] = failures
    return {"evidence": evidence, "failures": failures, "mesh_manifest": manifest}, files


def compute_clip(payload, source, root, settings):
    from pingpong_training.media.cliprender import render_intervals
    path = root / "clip.mp4"
    result = render_intervals(source, [tuple(v) for v in payload["intervals"]], path,
        pre_roll_ms=payload["pre_roll_ms"], post_roll_ms=payload["post_roll_ms"], reencode=True,
        ffmpeg_bin=settings.ffmpeg_bin, ffprobe_bin=settings.ffprobe_bin)
    return {"render": result.model_dump(mode="json")}, {"clip.mp4": path}


def compute_quiz(payload, source, root, settings):
    from spinread.product.quiz_generation import compute_candidates
    candidates, windows, impacts = compute_candidates(source, root, payload["duration_ms"], settings)
    return {"candidates": [asdict(c) for c in candidates], "windows": windows, "impacts": list(impacts)}, {}


def execute(request, root, *, source_path=None, settings=None):
    """Same code for isolated local verification and the Modal runtime."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    payload = request["payload"]
    if request["identity"]["request_hash"] != digest(payload):
        raise RemoteError("REQUEST_HASH_MISMATCH")
    settings = settings or Settings(_env_file=None)
    settings = settings.model_copy(update={"tmp_dir": str(root / "scratch"),
        **{k: v for k, v in payload.get("settings", {}).items() if k in COMPUTE_SETTINGS}})
    Path(settings.tmp_dir).mkdir(parents=True, exist_ok=True)
    source = root / "source.video"
    spec = payload["source"]
    if source_path is not None:
        from spinread.core.storage import file_sha256
        shutil.copyfile(source_path, source)
        if source.stat().st_size != spec["bytes"] or file_sha256(str(source)) != spec["sha256"]:
            raise RemoteError("INPUT_HASH_MISMATCH")
    else:
        fetch(request["source_url"], source, max_bytes=spec["bytes"], expected_hash=spec["sha256"])
    operation = payload["operation"]
    handler = {"stage": compute_stage, "replay": compute_replay, "clip": compute_clip, "quiz": compute_quiz}.get(operation)
    if handler is None:
        raise RemoteError("UNSUPPORTED_OPERATION")
    result, files = handler(payload, source, root, settings)
    bundle = root / "result.zip"
    pack(bundle, request["identity"], result, files, max_bytes=request["max_bytes"])
    return bundle


def invoke(request):
    """Modal function body. Never log requests, signed URLs or raw exceptions."""
    import httpx
    from spinread.remote.protocol import checked_url
    with tempfile.TemporaryDirectory(prefix="rallyloop-compute-") as directory:
        try:
            bundle = execute(request, directory)
            with bundle.open("rb") as stream:
                response = httpx.put(checked_url(request["output_url"]), content=stream,
                    headers={"Content-Length": str(bundle.stat().st_size)}, timeout=300, follow_redirects=False)
            response.raise_for_status()
            return {"status": "SUCCEEDED"}
        except Exception:
            # Full diagnostics can be added through a separately scrubbed event
            # sink; SDK result/logs must not leak presigned capability URLs.
            return {"status": "FAILED", "error_code": "REMOTE_COMPUTE_FAILED"}
