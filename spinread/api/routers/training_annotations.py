"""Video-level human training chapters, independent of generated timelines."""
from typing import Literal
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session
from spinread.api.deps import get_current_user, get_db, get_owned_video
from spinread.api.errors import ApiError
from spinread.core.access import viewable_video, audit
from spinread.core.models import TrainingAnnotationRevision, User, Video

router = APIRouter(prefix="/api/videos", tags=["training annotations"])

class Action(BaseModel):
    hand: Literal["FOREHAND", "BACKHAND", "UNSPECIFIED"] = "UNSPECIFIED"
    stroke: str = Field(default="", max_length=80)
    incoming_spin: Literal["TOPSPIN", "BACKSPIN", "SIDESPIN", "NO_SPIN", "UNKNOWN"] = "UNKNOWN"
    movement: str = Field(default="", max_length=120)

class Segment(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    start_ms: int = Field(ge=0, strict=True)
    end_ms: int = Field(gt=0, strict=True)
    title: str = Field(min_length=1, max_length=100)
    feeding: Literal["RALLY", "MULTIBALL", "SERVE_RECEIVE", "OTHER"]
    movement: Literal["FIXED", "TWO_POINT", "MOVING", "UNSPECIFIED"] = "UNSPECIFIED"
    target: Literal["NEAR", "FAR", "LEFT", "RIGHT", "UNSPECIFIED"] = "UNSPECIFIED"
    actions: list[Action] = Field(default_factory=list, max_length=20)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_range(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("训练段结束必须晚于开始")
        self.title = self.title.strip()
        if not self.title:
            raise ValueError("请填写训练名称")
        return self

class SaveAnnotations(BaseModel):
    base_version: int = Field(ge=0, strict=True)
    segments: list[Segment] = Field(max_length=500)

    @model_validator(mode="after")
    def validate_segments(self):
        self.segments.sort(key=lambda segment: segment.start_ms)
        if len({s.id for s in self.segments}) != len(self.segments):
            raise ValueError("训练段 ID 不可重复")
        if any(a.end_ms > b.start_ms for a, b in zip(self.segments, self.segments[1:])):
            raise ValueError("人工训练段不可重叠")
        return self

def latest(db, video_id):
    return db.scalar(select(TrainingAnnotationRevision).where(
        TrainingAnnotationRevision.video_id == video_id).order_by(TrainingAnnotationRevision.version.desc()).limit(1))

def output(row, video_id):
    return {"video_id": video_id, "version": row.version if row else 0,
        "segments": row.segments if row else [], "updated_by": row.author_id if row else None}

@router.get("/{video_id}/training-annotations")
def read_annotations(video_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    video = viewable_video(video_id, db, user)
    return output(latest(db, video.id), video.id)

@router.put("/{video_id}/training-annotations")
def save_annotations(video_id: str, body: SaveAnnotations, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # The video row serializes first creation as well as later revisions.
    db.scalar(select(Video).where(Video.id == video_id).with_for_update())
    video = get_owned_video(video_id, db, user)
    row = latest(db, video.id)
    version = row.version if row else 0
    if body.base_version != version:
        raise ApiError(409, "ANNOTATION_VERSION_CONFLICT", "训练标注已被更新，请查看最新区间后重试")
    if not video.duration_ms or any(s.end_ms > video.duration_ms for s in body.segments):
        raise ApiError(422, "INVALID_TRAINING_RANGE", "训练区间超出视频时长")
    revision = TrainingAnnotationRevision(video_id=video.id, version=version + 1,
        segments=[s.model_dump() for s in body.segments], author_id=user.id)
    db.add(revision)
    audit(db, user.id, "TRAINING_ANNOTATION_SAVED", video.id, video.id, version=version + 1)
    db.flush()
    return output(revision, video.id)
