"""metrics/reports/findings/exports (LLD §3.5 subset)

Revision ID: 0002_reports_exports
Revises: 0001_initial
Create Date: 2026-09-21
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_reports_exports"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pipeline_runs.scope: JSON list of stage names for partial (correction)
    # runs; NULL = full DAG. Lets orchestrator.tick re-derive the run's scope
    # on every invocation.
    op.add_column("pipeline_runs", sa.Column("scope", sa.JSON(), nullable=True))

    op.create_table(
        "metric_values",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("video_id", sa.Text(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column(
            "pipeline_run_id", sa.Text(), sa.ForeignKey("pipeline_runs.id"), nullable=False
        ),
        sa.Column("timeline_version", sa.Integer(), nullable=False),
        sa.Column("metric_name", sa.Text(), nullable=False),
        sa.Column("metric_version", sa.Text(), nullable=False),
        sa.Column("value", sa.JSON(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "video_id", "timeline_version", "metric_name", "metric_version",
            name="uq_metric_values_video_tl_name_version",
        ),
    )

    op.create_table(
        "analysis_reports",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("video_id", sa.Text(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("timeline_version", sa.Integer(), nullable=False),
        sa.Column("metric_versions", sa.JSON(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("structured", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "findings",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "report_id", sa.Text(), sa.ForeignKey("analysis_reports.id"), nullable=False
        ),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column("evidence_intervals", sa.JSON(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("priority_score", sa.Float(), nullable=False),
        sa.Column("limitations", sa.JSON(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
    )

    op.create_table(
        "export_manifests",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("video_id", sa.Text(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("timeline_version", sa.Integer(), nullable=False),
        sa.Column("intervals", sa.JSON(), nullable=False),
        sa.Column("render_preset", sa.Text(), nullable=False),
        sa.Column("idempotency_hash", sa.Text(), nullable=False),
        sa.Column(
            "asset_id", sa.Text(), sa.ForeignKey("media_assets.id"), nullable=True
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_hash", name="uq_export_manifests_idem_hash"),
    )


def downgrade() -> None:
    for table in ("export_manifests", "findings", "analysis_reports", "metric_values"):
        op.drop_table(table)
    op.drop_column("pipeline_runs", "scope")
