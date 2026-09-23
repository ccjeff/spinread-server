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
    training_context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    context_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
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
    scope: Mapped[list | None] = mapped_column(JSON, nullable=True)  # NULL = full DAG
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


class MetricValue(Base):
    __tablename__ = "metric_values"
    __table_args__ = (
        UniqueConstraint(
            "video_id", "timeline_version", "metric_name", "metric_version",
            name="uq_metric_values_video_tl_name_version",
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("met"))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    pipeline_run_id: Mapped[str] = mapped_column(ForeignKey("pipeline_runs.id"), nullable=False)
    timeline_version: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_name: Mapped[str] = mapped_column(Text, nullable=False)
    metric_version: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AnalysisReport(Base):
    __tablename__ = "analysis_reports"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("rep"))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    timeline_version: Mapped[int] = mapped_column(Integer, nullable=False)
    metric_versions: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="DRAFT")
    structured: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Finding(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("fnd"))
    report_id: Mapped[str] = mapped_column(ForeignKey("analysis_reports.id"), nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    observation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_intervals: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    priority_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    limitations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="PUBLISHED")


class ExportManifest(Base):
    __tablename__ = "export_manifests"
    __table_args__ = (
        UniqueConstraint("idempotency_hash", name="uq_export_manifests_idem_hash"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("exp"))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # CLIP | HIGHLIGHT
    timeline_version: Mapped[int] = mapped_column(Integer, nullable=False)
    intervals: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    render_preset: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_hash: Mapped[str] = mapped_column(Text, nullable=False)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("media_assets.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QuizGeneration(Base):
    __tablename__ = "quiz_generations"
    __table_args__ = (UniqueConstraint("video_id", "timeline_id", "detector_version"),)
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("qgen"))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    timeline_id: Mapped[str] = mapped_column(ForeignKey("timelines.id"), nullable=False)
    detector_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="QUEUED")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    limitations: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QuizItem(Base):
    __tablename__ = "quiz_items"
    __table_args__ = (
        UniqueConstraint("family_id", "version"),
        UniqueConstraint("generation_id", "candidate_index"),
        CheckConstraint("start_ms >= 0 AND start_ms < contact_ms AND contact_ms < pause_ms AND pause_ms < end_ms", name="ck_quiz_bounds"),
        Index("ix_quiz_video_current", "video_id", "is_current"),
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("quiz"))
    family_id: Mapped[str] = mapped_column(Text, nullable=False, default=lambda: new_id("qfam"))
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    is_current: Mapped[bool] = mapped_column(nullable=False, default=True)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    timeline_id: Mapped[str] = mapped_column(ForeignKey("timelines.id"), nullable=False)
    generation_id: Mapped[str | None] = mapped_column(ForeignKey("quiz_generations.id"), nullable=True)
    candidate_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    contact_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pause_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    approval: Mapped[str] = mapped_column(Text, nullable=False, default="DRAFT")
    provenance: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    answer_basis: Mapped[dict] = mapped_column(JSON, nullable=False, default=lambda: {"recommended_confirmed": False, "actual_outcome": "UNKNOWN"})
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class QuizAttempt(Base):
    __tablename__ = "quiz_attempts"
    __table_args__ = (UniqueConstraint("user_id", "request_id"),)
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("qat"))
    quiz_item_id: Mapped[str] = mapped_column(ForeignKey("quiz_items.id"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    request_id: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[dict] = mapped_column(JSON, nullable=False)
    confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    elapsed_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    scored: Mapped[bool] = mapped_column(nullable=False, default=False)
    feedback_version: Mapped[str] = mapped_column(Text, nullable=False, default="observation-1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PracticeWindow(Base):
    __tablename__ = "practice_windows"
    __table_args__ = (
        UniqueConstraint("video_id", "timeline_id", "feature_version", "start_ms"),
        CheckConstraint("start_ms >= 0 AND end_ms > start_ms"),
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("pwin"))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    timeline_id: Mapped[str] = mapped_column(ForeignKey("timelines.id"), nullable=False)
    feature_version: Mapped[str] = mapped_column(Text, nullable=False)
    start_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    end_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    features: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    label_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    reviews: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


class CoachGrant(Base):
    __tablename__ = "coach_grants"
    __table_args__ = (UniqueConstraint("player_id", "coach_id"),)
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("cg"))
    player_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    coach_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    status: Mapped[str] = mapped_column(Text, default="ACTIVE")
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ConsentGrant(Base):
    __tablename__ = "consent_grants"
    __table_args__ = (UniqueConstraint("video_id", "purpose", "granted_to"),)
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("cns"))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    subject_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    granted_to: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    state: Mapped[str] = mapped_column(Text, default="ACTIVE")
    version: Mapped[int] = mapped_column(Integer, default=1)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("aud"))
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"))
    video_id: Mapped[str | None] = mapped_column(ForeignKey("videos.id"))
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("user_id", "key"),)
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("idem"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class TrainingPlan(Base):
    __tablename__ = "training_plans"
    __table_args__ = (Index("uq_active_training_plan", "user_id", unique=True,
        postgresql_where=text("state = 'ACTIVE'"), sqlite_where=text("state = 'ACTIVE'")),)
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("plan"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    source_report_id: Mapped[str] = mapped_column(ForeignKey("analysis_reports.id"), nullable=False)
    state: Mapped[str] = mapped_column(Text, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanItem(Base):
    __tablename__ = "plan_items"
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("pi"))
    plan_id: Mapped[str] = mapped_column(ForeignKey("training_plans.id"), nullable=False)
    source_report_id: Mapped[str] = mapped_column(ForeignKey("analysis_reports.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    finding_ids: Mapped[list] = mapped_column(JSON, default=list)
    drill: Mapped[dict] = mapped_column(JSON, default=dict)
    retest: Mapped[dict] = mapped_column(JSON, default=dict)
    source: Mapped[str] = mapped_column(Text, default="SYSTEM")
    locked_by_coach: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(Text, default="ACTIVE")
    player_note: Mapped[str] = mapped_column(Text, default="")
    history: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PlanItemRetest(Base):
    __tablename__ = "plan_item_retests"
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("rt"))
    plan_item_id: Mapped[str] = mapped_column(ForeignKey("plan_items.id"), nullable=False)
    plan_item_version: Mapped[int] = mapped_column(Integer, nullable=False)
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    report_id: Mapped[str] = mapped_column(ForeignKey("analysis_reports.id"), nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReviewRequest(Base):
    __tablename__ = "review_requests"
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("rev"))
    video_id: Mapped[str] = mapped_column(ForeignKey("videos.id"), nullable=False)
    coach_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    timeline_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="OPEN")
    version: Mapped[int] = mapped_column(Integer, default=1)
    question: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CoachFeedback(Base):
    __tablename__ = "coach_feedback"
    id: Mapped[str] = mapped_column(Text, primary_key=True, default=lambda: new_id("fb"))
    review_request_id: Mapped[str] = mapped_column(ForeignKey("review_requests.id"), nullable=False)
    timeline_item_id: Mapped[str | None] = mapped_column(ForeignKey("timeline_items.id"))
    start_ms: Mapped[int | None] = mapped_column(BigInteger)
    end_ms: Mapped[int | None] = mapped_column(BigInteger)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
