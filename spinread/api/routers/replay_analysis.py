"""Owner-requested supplemental analysis of existing, unchanged videos."""
from typing import Literal
from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from spinread.api.deps import get_current_user, get_db, get_owned_video
from spinread.api.errors import ApiError, not_found
from spinread.config import Settings, get_settings
from spinread.core.access import viewable_video, audit
from spinread.core.models import ReplayAnalysis, Timeline, TimelineActivePointer, TimelineItem, User, Video
from spinread.core.mutations import mutate
from spinread.core.queue import enqueue
from spinread.product.replay_analysis import engines

router = APIRouter(prefix="/api/videos", tags=["replay-analysis"])


class PlayerInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hand: Literal["left", "right"]
    kit: Literal["red", "green", "blue", "yellow", "white", "black"]
    height: float | None = Field(None, ge=100, le=230)
    mass: float | None = Field(None, ge=20, le=200)


class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timeline_version: int = Field(ge=1)
    engine: Literal["local", "pingpong3d"] = "local"
    players: list[PlayerInput] | None = Field(None, min_length=2, max_length=2)

    @model_validator(mode="after")
    def players_needed(self):
        if self.engine == "pingpong3d" and (not self.players or self.players[0].kit == self.players[1].kit):
            raise ValueError("Pingpong3D needs both players' handedness and distinct shirt colors")
        return self


def active_timeline(db, video, version, lock=False):
    query = select(TimelineActivePointer).where(TimelineActivePointer.video_id == video.id)
    pointer = db.scalar(query.with_for_update() if lock else query)
    timeline = db.get(Timeline, pointer.timeline_id) if pointer else None
    if timeline is None:
        raise not_found("Timeline not available")
    if timeline.version != version:
        raise ApiError(409, "TIMELINE_VERSION_CONFLICT", "时间线已更新，请刷新。")
    return timeline


def output(row, settings):
    return {"id": row.id if row else None, "state": row.state if row else "NONE",
            "engine": row.engine if row else None, "error_code": row.error_code if row else None,
            "available_engines": engines(settings)}


@router.get("/{video_id}/replay-analysis")
def get_analysis(video_id: str, timeline_version: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user), settings: Settings = Depends(get_settings)):
    video = viewable_video(video_id, db, user)
    timeline = active_timeline(db, video, timeline_version)
    row = db.scalar(select(ReplayAnalysis).where(ReplayAnalysis.video_id == video.id,
        ReplayAnalysis.timeline_id == timeline.id).order_by(ReplayAnalysis.created_at.desc()))
    return output(row, settings)


@router.post("/{video_id}/replay-analysis", status_code=202)
def create_analysis(video_id: str, body: AnalysisInput, db: Session = Depends(get_db),
                    user: User = Depends(get_current_user), settings: Settings = Depends(get_settings),
                    idempotency_key: str = Header(min_length=8, max_length=200)):
    video = get_owned_video(video_id, db, user)
    def action():
        db.scalar(select(Video).where(Video.id == video.id).with_for_update())
        timeline = active_timeline(db, video, body.timeline_version, lock=True)
        if body.engine not in engines(settings):
            raise ApiError(409, "ANALYSIS_ENGINE_UNAVAILABLE", "该分析引擎尚未配置模型与运行环境。")
        pending = db.scalar(select(ReplayAnalysis).where(ReplayAnalysis.video_id == video.id,
            ReplayAnalysis.state.in_(["QUEUED", "RUNNING"])))
        if pending:
            raise ApiError(409, "ANALYSIS_ACTIVE", "该视频已有补充分析任务。")
        if db.scalar(select(TimelineItem.id).where(TimelineItem.timeline_id == timeline.id,
                TimelineItem.type == "RALLY", TimelineItem.status == "ACTIVE").limit(1)) is None:
            raise ApiError(422, "NO_RALLIES", "请先确认至少一个回合。")
        row = ReplayAnalysis(video_id=video.id, timeline_id=timeline.id, timeline_version=timeline.version,
            engine=body.engine, options={"players": [p.model_dump(exclude_none=True) for p in body.players] if body.players else []})
        db.add(row); db.flush()
        enqueue(db, "REPLAY_ANALYSIS", {"analysis_id": row.id}, max_attempts=1)
        audit(db, user.id, "REPLAY_ANALYSIS_REQUESTED", row.id, video.id)
        return output(row, settings)
    return mutate(db, user, idempotency_key, "replay-analysis:" + video_id, body.model_dump(), action)
