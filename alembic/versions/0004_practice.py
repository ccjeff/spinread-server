"""Reviewed practice contexts, isolated by recording and timeline."""
from alembic import op
import sqlalchemy as sa

revision = "0004_practice"
down_revision = "0003_quizzes"
branch_labels = depends_on = None


def upgrade():
    op.create_table("practice_windows",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("video_id", sa.Text(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("timeline_id", sa.Text(), sa.ForeignKey("timelines.id"), nullable=False),
        sa.Column("feature_version", sa.Text(), nullable=False),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_ms", sa.BigInteger(), nullable=False),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("label", sa.Text()),
        sa.Column("label_source", sa.Text()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("reviews", sa.JSON(), nullable=False),
        sa.UniqueConstraint("video_id", "timeline_id", "feature_version", "start_ms"),
        sa.CheckConstraint("start_ms >= 0 AND end_ms > start_ms"))


def downgrade():
    op.drop_table("practice_windows")
