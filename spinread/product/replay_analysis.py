"""Append-only supplemental analysis; never regenerates the user's timeline."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import shutil

from sqlalchemy import select

from spinread.core.models import (Artifact, MediaAsset, PipelineRun, ReplayAnalysis,
                                  TimelineActivePointer, TimelineItem, Video, utcnow)
from spinread.pipeline.vision import vision_config
from spinread.product.pingpong3d_runner import configured, run_match, UpstreamUnavailable

log = logging.getLogger(__name__)


def has_pose(evidence):
    return any(any(any(p is not None for p in person.get("world", [])) for person in frame.get("people", []))
               for frame in (evidence.get("pose") or {}).get("frames", []))


def placement_fingerprint(evidence):
    """Only inputs consumed by placement review, excluding independent 3D views."""
    values = {key: evidence.get(key) for key in
              ("duration_ms", "frames", "contacts", "strokes", "placements")}
    return hashlib.sha256(json.dumps(values, sort_keys=True, ensure_ascii=False,
        allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def merge_evidence(base, extra):
    """Merge independently produced modalities without erasing successful ones.

    All inputs are already scoped to the same video/timeline. Geometry is never
    transformed here; each modality retains its own coordinate frame/provenance.
    """
    if not base:
        return extra
    result = {**base, **extra}
    provenance = dict(base.get("modality_provenance", {}))
    fields = ("frames", "pose", "mesh", "strokes", "placements", "ball_3d", "biomechanics")
    for key in fields:
        value = extra.get(key)
        usable = (has_pose(extra) if key == "pose" else
                  bool((value or {}).get("chunks")) if key == "mesh" else
                  bool((value or {}).get("segments")) if key == "ball_3d" else
                  any(f[1] is not None for f in (value or [])) if key == "frames" else bool(value))
        if usable:
            provenance[key] = extra.get("provenance") or {"provider": "SPINREAD_LOCAL", "model": extra.get("model", {})}
        elif key in base:
            result[key] = base[key]
            provenance.setdefault(key, base.get("provenance") or {"provider": "SPINREAD_LOCAL", "model": base.get("model", {})})
    # Imported strokes and their contacts are a single clock association. Mixing
    # nearby acoustic contacts into that set creates artificial ambiguities.
    if extra.get("strokes"):
        result["contacts"] = extra.get("contacts", [])
    elif base.get("strokes"):
        result["contacts"] = base.get("contacts", [])
    else:
        result["contacts"] = sorted({c.get("source_time_ms", c["time_ms"]): c for c in
            [*base.get("contacts", []), *extra.get("contacts", [])]}.values(), key=lambda c:c["time_ms"])
    result["modality_provenance"] = provenance
    result["limitations"] = list(dict.fromkeys([*base.get("limitations", []), *extra.get("limitations", [])]))
    caps = dict(base.get("capabilities", {}))
    for key, value in extra.get("capabilities", {}).items():
        if key not in caps or value not in {"UNAVAILABLE", "UNSUPPORTED", "UNKNOWN"}:
            caps[key] = value
    result["capabilities"] = caps
    return result


def engines(settings):
    available = []
    if any(path and Path(path).is_file() for path in (settings.blurball_weights, settings.pose_weights)):
        available.append("local")
    if configured(settings):
        available.append("pingpong3d")
    from spinread.product.nlf_analysis import status
    if status(settings).get("available"):
        available.append("nlf")
    return available


def analysis_evidence(db, s3, video, timeline_id, cache, *, public=True):
    row = db.scalar(select(ReplayAnalysis).where(ReplayAnalysis.video_id == video.id,
        ReplayAnalysis.timeline_id == timeline_id, ReplayAnalysis.state.in_(["READY", "PARTIAL_READY"]),
        ReplayAnalysis.artifact_id.is_not(None)).order_by(ReplayAnalysis.created_at.desc(), ReplayAnalysis.id.desc()))
    if row is None:
        return None
    artifact = db.get(Artifact, row.artifact_id)
    if artifact is None or artifact.video_id != video.id or artifact.stage != "REPLAY_ANALYSIS":
        return None
    key = ("analysis", artifact.id)
    if key not in cache:
        payload = json.loads(s3.get_bytes(artifact.object_key))
        if payload.get("video_id") != video.id or payload.get("timeline_id") != timeline_id:
            return None
        evidence = payload["replay"]
        if evidence.get("strokes"):
            from pingpong_training.analysis.pingpong3d import align_pingpong3d_contacts
            rows = db.scalars(select(TimelineItem).where(TimelineItem.timeline_id == timeline_id,
                TimelineItem.status == "ACTIVE")).all()
            rallies = [{"id": r.id, "start_ms": r.start_ms, "end_ms": r.end_ms,
                        "hits_ms": [h.start_ms for h in rows if h.parent_id == r.id and h.type == "HIT_CANDIDATE"]}
                       for r in rows if r.type == "RALLY"]
            evidence = align_pingpong3d_contacts(evidence, rallies)
        cache[key] = evidence
    evidence = cache[key]
    if public and evidence.get("mesh"):
        from spinread.product.nlf_analysis import public_mesh
        return {**evidence, "mesh": public_mesh(evidence["mesh"], video.id, row.timeline_version, artifact.id)}
    return evidence


def _local(source, work, settings, duration):
    from pingpong_training.analysis.replay import replay_evidence
    evidence = replay_evidence(None, [], duration)
    failures = []
    config = vision_config(settings)
    if config is not None:
        try:
            from pingpong_training.analysis.visual_events import analyze_visual_video
            result, track, _ = analyze_visual_video(source, work / "vision", config,
                ffmpeg_bin=settings.ffmpeg_bin, ffprobe_bin=settings.ffprobe_bin)
            evidence = replay_evidence(track, result.contacts, duration)
        except Exception:
            log.exception("Supplemental ball analysis failed")
            failures.append("BALL_ANALYSIS_FAILED")
    if settings.pose_weights:
        try:
            from pingpong_training.analysis.pose import PoseConfig, infer_pose
            evidence["pose"] = infer_pose(source, work / "pose", PoseConfig(settings.pose_weights, fps=settings.pose_fps),
                ffmpeg_bin=settings.ffmpeg_bin, ffprobe_bin=settings.ffprobe_bin)
            evidence["capabilities"]["pose_3d"] = "MODEL_ESTIMATE" if has_pose(evidence) else "UNAVAILABLE"
            if not has_pose(evidence):
                failures.append("POSE_NO_DETECTIONS")
        except Exception:
            log.exception("Supplemental pose analysis failed")
            failures.append("POSE_ANALYSIS_FAILED")
    evidence["analysis_warnings"] = failures
    return evidence, failures


def publish_analysis(db, s3, analysis, video, evidence, *, mesh_assets=None):
    """Publish new immutable bytes and link only this supplemental analysis row."""
    run = db.scalar(select(PipelineRun).where(PipelineRun.video_id == video.id).order_by(PipelineRun.created_at.desc()))
    if run is None:
        raise UpstreamUnavailable("PIPELINE_HISTORY_MISSING")
    previous = analysis_evidence(db, s3, video, analysis.timeline_id, {}, public=False)
    evidence = merge_evidence(previous, evidence)
    parent = db.scalar(select(ReplayAnalysis).where(ReplayAnalysis.video_id == video.id,
        ReplayAnalysis.timeline_id == analysis.timeline_id,
        ReplayAnalysis.state.in_(["READY", "PARTIAL_READY"]),
        ReplayAnalysis.artifact_id.is_not(None)).order_by(
            ReplayAnalysis.created_at.desc(), ReplayAnalysis.id.desc()))
    parent_artifact = db.get(Artifact, parent.artifact_id) if parent else None
    # Enriching a separate modality does not change the pixels/events a landing
    # was reviewed against. Prove this before retaining that review's binding;
    # never rewrite the user's stored review or accept client-supplied lineage.
    metrics = {"engine": analysis.engine}
    if previous and placement_fingerprint(previous) == placement_fingerprint(evidence):
        artifact = parent_artifact
        if artifact and artifact.video_id == video.id and artifact.stage == "REPLAY_ANALYSIS":
            ancestors = (artifact.metrics or {}).get("placement_review_ancestor_ids", [])
            metrics["placement_review_ancestor_ids"] = list(dict.fromkeys(
                [artifact.id, *[v for v in ancestors if isinstance(v, str)]]))[:256]
            metrics["placement_fingerprint"] = placement_fingerprint(evidence)
    if mesh_assets is not None:
        metrics["mesh_assets"] = mesh_assets
    elif previous and evidence.get("mesh") == previous.get("mesh") and parent_artifact:
        metrics["mesh_assets"] = (parent_artifact.metrics or {}).get("mesh_assets", {})
    payload = {"video_id": video.id, "timeline_id": analysis.timeline_id,
               "timeline_version": analysis.timeline_version, "analysis_id": analysis.id, "replay": evidence}
    data = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    key = f"users/{video.owner_id}/videos/{video.id}/replay/{analysis.id}/evidence.json"
    s3.put_bytes(key, data, content_type="application/json")
    artifact = Artifact(video_id=video.id, pipeline_run_id=run.id, stage="REPLAY_ANALYSIS",
        stage_version="pingpong3d-bridge-1", object_key=key, content_hash=hashlib.sha256(data).hexdigest(),
        metrics=metrics, limitations=evidence.get("limitations", []))
    db.add(artifact); db.flush()
    analysis.artifact_id = artifact.id


def run_replay_analysis(db, settings, s3, job):
    analysis = db.get(ReplayAnalysis, job.payload["analysis_id"])
    if analysis is None or analysis.state not in {"QUEUED", "RUNNING"}:
        return "done"
    video = db.get(Video, analysis.video_id)
    pointer = db.get(TimelineActivePointer, analysis.video_id)
    if video is None or video.deleted_at is not None:
        analysis.state = "FAILED"; analysis.error_code = "VIDEO_UNAVAILABLE"; db.commit(); return "done"
    if pointer is None or pointer.timeline_id != analysis.timeline_id:
        analysis.state = "STALE"; analysis.error_code = "TIMELINE_CHANGED"; db.commit(); return "done"
    original = db.scalar(select(MediaAsset).where(MediaAsset.video_id == video.id,
        MediaAsset.class_ == "ORIGINAL", MediaAsset.status == "ACTIVE"))
    if original is None:
        analysis.state = "FAILED"; analysis.error_code = "ORIGINAL_UNAVAILABLE"; db.commit(); return "done"
    analysis.state = "RUNNING"; analysis.updated_at = utcnow(); db.commit()
    root = Path(settings.tmp_dir) / "replay-analysis" / analysis.id
    root.mkdir(parents=True, exist_ok=True)
    source = root / "original.video"
    try:
        s3.download_file(original.object_key, str(source))
        failures = []
        mesh_manifest = None
        if analysis.engine == "nlf":
            from spinread.product.nlf_analysis import run as run_nlf_evidence
            evidence, mesh_manifest = run_nlf_evidence(source, root / "nlf", settings,
                                                       original.content_hash, video.duration_ms)
            if not has_pose(evidence):
                failures.append("POSE_NO_DETECTIONS")
            evidence["analysis_warnings"] = failures
        elif analysis.engine == "pingpong3d":
            from pingpong_training.analysis.pingpong3d import load_pingpong3d
            rows = db.scalars(select(TimelineItem).where(TimelineItem.timeline_id == analysis.timeline_id,
                TimelineItem.type == "RALLY", TimelineItem.status == "ACTIVE")).all()
            directory = run_match(source, root / "match", settings,
                [{"start_ms": r.start_ms, "end_ms": r.end_ms} for r in rows], analysis.options["players"])
            evidence = load_pingpong3d(directory, video.duration_ms)
            if not has_pose(evidence):
                failures.append("POSE_UNAVAILABLE")
            if not evidence.get("placements"):
                failures.append("PLACEMENTS_UNAVAILABLE")
            if not (evidence.get("ball_3d") or {}).get("segments"):
                failures.append("BALL_3D_UNAVAILABLE")
            evidence["analysis_warnings"] = failures
        else:
            evidence, failures = _local(source, root, settings, video.duration_ms)
        # Lock the video and current pointer at publication, just like timeline edits.
        db.expire_all()
        video = db.scalar(select(Video).where(Video.id == analysis.video_id).with_for_update())
        pointer = db.scalar(select(TimelineActivePointer).where(TimelineActivePointer.video_id == analysis.video_id).with_for_update())
        if video.deleted_at is not None:
            raise UpstreamUnavailable("VIDEO_UNAVAILABLE")
        if not pointer or pointer.timeline_id != analysis.timeline_id:
            analysis.state = "STALE"; analysis.error_code = "TIMELINE_CHANGED"
        else:
            available = bool(has_pose(evidence) or evidence.get("placements") or (evidence.get("ball_3d") or {}).get("segments")
                             or any(f[1] is not None for f in evidence.get("frames", [])))
            if not available:
                raise UpstreamUnavailable("NO_USABLE_EVIDENCE")
            mesh_assets = None
            if mesh_manifest and (evidence.get("mesh") or {}).get("chunks"):
                from spinread.product.nlf_analysis import publish_mesh
                mesh_assets = publish_mesh(s3, video, analysis, mesh_manifest, root / "nlf")
            publish_analysis(db, s3, analysis, video, evidence, mesh_assets=mesh_assets)
            analysis.state = "PARTIAL_READY" if failures else "READY"
            analysis.error_code = failures[0] if failures else None
        analysis.updated_at = utcnow(); db.commit()
    except Exception as exc:
        log.exception("Supplemental analysis %s failed", analysis.id)
        db.rollback()
        analysis = db.get(ReplayAnalysis, job.payload["analysis_id"])
        analysis.state = "FAILED"
        analysis.error_code = exc.code if isinstance(exc, UpstreamUnavailable) else "ANALYSIS_FAILED"
        analysis.updated_at = utcnow(); db.commit()
    finally:
        # Only our derived scratch data, never the original object or user records.
        if analysis.engine == 'nlf' and analysis.state == 'FAILED' and (root / 'nlf' / 'manifest.json').is_file():
            # Operators can resume the hash-bound checkpoint through the NLF CLI.
            # A new UI job is still a fresh request, not an implicit resume.
            source.unlink(missing_ok=True)
        else:
            shutil.rmtree(root, ignore_errors=True)
    return "done"
