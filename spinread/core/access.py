"""Scoped coach reads; relationship AND per-video consent are required."""
from sqlalchemy import select
from spinread.core.models import AuditEvent, CoachGrant, ConsentGrant, User, Video
from spinread.api.errors import forbidden, not_found


def audit(db, actor_id, kind, resource_id, video_id=None, **details):
    db.add(AuditEvent(actor_id=actor_id, kind=kind, resource_id=resource_id,
        video_id=video_id, details=details))


def coach_can_view(db, user, video, lock=False):
    if user.role != "COACH":
        return False
    relation_query = select(CoachGrant.id).where(CoachGrant.player_id == video.owner_id,
        CoachGrant.coach_id == user.id, CoachGrant.status == "ACTIVE")
    relation = db.scalar(relation_query.with_for_update() if lock else relation_query)
    consent_query = select(ConsentGrant.id).where(ConsentGrant.video_id == video.id,
        ConsentGrant.granted_to == user.id, ConsentGrant.purpose == "COACH_VIEW", ConsentGrant.state == "ACTIVE")
    consent = db.scalar(consent_query.with_for_update() if lock else consent_query)
    return bool(relation and consent)


def viewable_video(video_id, db, user):
    v = db.get(Video, video_id)
    if v is None or v.deleted_at is not None:
        raise not_found("视频不存在或已删除")
    if v.owner_id != user.id and not coach_can_view(db, user, v):
        raise forbidden("需要球员关系授权和此视频的教练访问许可")
    return v


def require_coach(user):
    if user.role != "COACH":
        raise forbidden("此操作需要教练身份")
