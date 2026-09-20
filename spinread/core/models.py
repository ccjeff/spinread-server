"""SQLAlchemy declarative models — LLD §3 subset for the MLP upload->timeline chain."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("usr"))
    email: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False, default="USER")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Video(Base):
    __tablename__ = "videos"
    __table_args__ = (
        Index("ix_videos_owner_created", "owner_id", text("created_at DESC")),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("vid"))
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="UPLOAD_PENDING")
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    session_type: Mapped[str] = mapped_column(Text, nullable=False, default="TRAINING")
    target_player: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    probe: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MediaAsset(Base):
    __tablename__ = "media_assets"
    __table_args__ = (
        UniqueConstraint("video_id", "class", "media_version", "object_key"),
        Index("ix_media_assets_video_class_status", "video_id", "class", "status"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("med"))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    class_: Mapped[str] = mapped_column("class", Text, nullable=False)
    media_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="PENDING")
    manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("upl"))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    provider_upload_id: Mapped[str] = mapped_column(Text, nullable=False)
    expected_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="OPEN")
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        Index("ix_pipeline_runs_video_created", "video_id", text("created_at DESC")),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("run"))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False, default="UPLOAD")
    state: Mapped[str] = mapped_column(Text, nullable=False, default="RUNNING")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StageRun(Base):
    __tablename__ = "stage_runs"
    __table_args__ = (
        UniqueConstraint("pipeline_run_id", "stage", "idempotency_key"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("stg"))
    pipeline_run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    stage_version: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="QUEUED")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_artifact_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    output_artifact_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        UniqueConstraint("content_hash"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("art"))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    pipeline_run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=False)
    stage: Mapped[str] = mapped_column(Text, nullable=False)
    stage_version: Mapped[str] = mapped_column(Text, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    limitations: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "ix_jobs_kind_run_at_open",
            "kind",
            "run_at",
            postgresql_where=text("done_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("job"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    claimed_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)


class Timeline(Base):
    __tablename__ = "timelines"
    __table_args__ = (
        UniqueConstraint("video_id", "version"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("tl"))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="BUILDING")
    created_by: Mapped[str] = mapped_column(Text, nullable=False, default="MODEL")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TimelineActivePointer(Base):
    __tablename__ = "timeline_active_pointers"

    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), primary_key=True)
    timeline_id: Mapped[str] = mapped_column(ForeignKey("timelines.id"), nullable=False)
    switched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TimelineItem(Base):
    __tablename__ = "timeline_items"
    __table_args__ = (
        CheckConstraint("end_ms > start_ms", name="ck_timeline_items_half_open"),
        Index("ix_timeline_items_timeline_start", "timeline_id", "start_ms"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("tli"))
    timeline_id: Mapped[str] = mapped_column(ForeignKey("timelines.id"), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("timeline_items.id"), nullable=True
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actor: Mapped[str | None] = mapped_column(Text, nullable=True)
    attributes: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    provenance: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="ACTIVE")
