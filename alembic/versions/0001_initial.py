"""initial schema (LLD §3 subset)

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-20
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_unique_constraint("uq_users_email", "users", ["email"])

    op.create_table(
        "videos",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("owner_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("session_type", sa.Text(), nullable=False),
        sa.Column("target_player", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.BigInteger(), nullable=True),
        sa.Column("probe", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_videos_owner_created", "videos", ["owner_id", sa.text("created_at DESC")]
    )

    op.create_table(
        "media_assets",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("video_id", sa.Text(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("class", sa.Text(), nullable=False),
        sa.Column("media_version", sa.Integer(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "video_id", "class", "media_version", "object_key",
            name="uq_media_assets_video_class_version_key",
        ),
    )
    op.create_index(
        "ix_media_assets_video_class_status",
        "media_assets",
        ["video_id", "class", "status"],
    )

    op.create_table(
        "upload_sessions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("video_id", sa.Text(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("provider_upload_id", sa.Text(), nullable=False),
        sa.Column("expected_bytes", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("video_id", sa.Text(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("pipeline_version", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_pipeline_runs_video_created",
        "pipeline_runs",
        ["video_id", sa.text("created_at DESC")],
    )

    op.create_table(
        "stage_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "pipeline_run_id", sa.Text(), sa.ForeignKey("pipeline_runs.id"), nullable=False
        ),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("stage_version", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("input_artifact_ids", sa.JSON(), nullable=False),
        sa.Column("output_artifact_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.UniqueConstraint(
            "pipeline_run_id", "stage", "idempotency_key", name="uq_stage_runs_run_stage_key"
        ),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("video_id", sa.Text(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column(
            "pipeline_run_id", sa.Text(), sa.ForeignKey("pipeline_runs.id"), nullable=False
        ),
        sa.Column("stage", sa.Text(), nullable=False),
        sa.Column("stage_version", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("limitations", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("content_hash", name="uq_artifacts_content_hash"),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_by", sa.Text(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("done_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_jobs_kind_run_at_open",
        "jobs",
        ["kind", "run_at"],
        postgresql_where=sa.text("done_at IS NULL"),
    )

    op.create_table(
        "timelines",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("video_id", sa.Text(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("video_id", "version", name="uq_timelines_video_version"),
    )

    op.create_table(
        "timeline_active_pointers",
        sa.Column(
            "video_id", sa.Text(), sa.ForeignKey("videos.id"), primary_key=True
        ),
        sa.Column("timeline_id", sa.Text(), sa.ForeignKey("timelines.id"), nullable=False),
        sa.Column("switched_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "timeline_items",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "timeline_id", sa.Text(), sa.ForeignKey("timelines.id"), nullable=False
        ),
        sa.Column(
            "parent_id", sa.Text(), sa.ForeignKey("timeline_items.id"), nullable=True
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_ms", sa.BigInteger(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("attributes", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.CheckConstraint("end_ms > start_ms", name="ck_timeline_items_half_open"),
    )
    op.create_index(
        "ix_timeline_items_timeline_start", "timeline_items", ["timeline_id", "start_ms"]
    )


def downgrade() -> None:
    for table in (
        "timeline_items",
        "timeline_active_pointers",
        "timelines",
        "jobs",
        "artifacts",
        "stage_runs",
        "pipeline_runs",
        "upload_sessions",
        "media_assets",
        "videos",
        "users",
    ):
        op.drop_table(table)
