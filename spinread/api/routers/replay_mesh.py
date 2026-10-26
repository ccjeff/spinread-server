"""SMPL bytes inherit the video's access, active timeline and evidence scope."""
import hashlib

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from spinread.api.deps import get_current_user, get_db, get_s3
from spinread.api.errors import not_found
from spinread.api.routers.replay_analysis import active_timeline
from spinread.core.access import viewable_video
from spinread.core.models import Artifact, ReplayAnalysis, User
from spinread.product.nlf_analysis import ASSET_ID, MAX_ASSET_BYTES

router = APIRouter(prefix="/api/videos", tags=["replay-mesh"])


@router.get("/{video_id}/replay-mesh/assets/{asset_id}")
def get_mesh_asset(video_id: str, asset_id: str, timeline_version: int, analysis_artifact_id: str,
                   db: Session = Depends(get_db), user: User = Depends(get_current_user), s3=Depends(get_s3)):
    video = viewable_video(video_id, db, user)
    timeline = active_timeline(db, video, timeline_version)
    if not ASSET_ID.fullmatch(asset_id):
        raise not_found("Mesh asset unavailable")
    analysis = db.scalar(select(ReplayAnalysis).where(ReplayAnalysis.video_id == video.id,
        ReplayAnalysis.timeline_id == timeline.id, ReplayAnalysis.artifact_id == analysis_artifact_id,
        ReplayAnalysis.state.in_(["READY", "PARTIAL_READY"])))
    artifact = db.get(Artifact, analysis.artifact_id) if analysis else None
    if not artifact or artifact.video_id != video.id or artifact.stage != "REPLAY_ANALYSIS":
        raise not_found("Mesh asset unavailable")
    spec = (artifact.metrics or {}).get("mesh_assets", {}).get(asset_id)
    if not isinstance(spec, dict):
        raise not_found("Mesh asset unavailable")
    size = spec.get("byte_length")
    key = spec.get("object_key", "")
    if (not isinstance(size, int) or not 0 < size <= MAX_ASSET_BYTES
            or not key.startswith(f"users/{video.owner_id}/videos/{video.id}/replay/")
            or ".." in key.split("/")):
        raise not_found("Mesh asset unavailable")
    data = s3.get_bytes(key)
    if len(data) != size or hashlib.sha256(data).hexdigest() != spec.get("sha256"):
        raise not_found("Mesh asset unavailable")
    return Response(data, media_type="application/octet-stream", headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
