"""Add remote execution receipts; no changes to existing video/training rows."""
from alembic import op
import sqlalchemy as sa

revision = "0010_remote_executions"
down_revision = "0009_replay_training_bridge"
branch_labels = depends_on = None


def upgrade():
    op.create_table("remote_executions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("job_id", sa.Text(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("call_id", sa.Text(), nullable=True),
        sa.Column("output_key", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("job_id", "operation"))


def downgrade():
    op.drop_table("remote_executions")
