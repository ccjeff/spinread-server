"""Pydantic request/response schemas (LLD §9)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    role: str = "USER"
    id: str
    email: str
    display_name: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class TargetPlayer(BaseModel):
    mode: Literal["NEAR", "FAR", "LEFT", "RIGHT"]


class CreateUploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    byte_size: int = Field(gt=0)
    content_type: str = "video/mp4"
    session_type: Literal["TRAINING", "MATCH"] = "TRAINING"
    target_player: TargetPlayer = TargetPlayer(mode="NEAR")
    recorded_at: datetime | None = None


class UploadPart(BaseModel):
    part_number: int
    presigned_url: str


class CreateUploadResponse(BaseModel):
    video_id: str
    upload_id: str
    part_size: int
    parts: list[UploadPart]
    expires_at: datetime


class CompletedPart(BaseModel):
    part_number: int
    etag: str


class CompleteUploadRequest(BaseModel):
    parts: list[CompletedPart]


class CompleteUploadResponse(BaseModel):
    video_id: str
    state: str


class UploadSessionOut(BaseModel):
    upload_id: str
    video_id: str
    state: str
    expected_bytes: int
    expires_at: datetime


class VideoListItem(BaseModel):
    id: str
    state: str
    filename: str
    session_type: str
    duration_ms: int | None
    created_at: datetime


class VideoOut(VideoListItem):
    target_player: dict[str, Any]
    recorded_at: datetime | None
    probe: dict[str, Any] | None


class StageStatus(BaseModel):
    stage: str
    status: str
    attempt: int
    error_code: str | None = None


class ProcessingStatusOut(BaseModel):
    state: str
    progress_pct: int
    stages: list[StageStatus]
    limitations: list[str] = []


class TimelineItemOut(BaseModel):
    item_id: str
    parent_id: str | None = None
    type: str
    start_ms: int
    end_ms: int
    actor: str | None
    confidence: float | None
    provenance: dict[str, Any]
    attributes: dict[str, Any]


class ActiveTimelineOut(BaseModel):
    timeline_id: str
    version: int
    video_duration_ms: int | None
    items: list[TimelineItemOut]
