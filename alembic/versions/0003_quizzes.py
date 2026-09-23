"""Private, versioned serve quizzes and append-only attempts."""
from alembic import op
import sqlalchemy as sa

revision = "0003_quizzes"
down_revision = "0002_reports_exports"
branch_labels = depends_on = None


def upgrade():
    op.create_table("quiz_generations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("video_id", sa.Text(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("timeline_id", sa.Text(), sa.ForeignKey("timelines.id"), nullable=False),
        sa.Column("detector_version", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error", sa.Text()),
        sa.Column("limitations", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("video_id", "timeline_id", "detector_version"))
    op.create_table("quiz_items",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("family_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("video_id", sa.Text(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("timeline_id", sa.Text(), sa.ForeignKey("timelines.id"), nullable=False),
        sa.Column("generation_id", sa.Text(), sa.ForeignKey("quiz_generations.id")),
        sa.Column("candidate_index", sa.Integer()),
        *[sa.Column(n, sa.BigInteger(), nullable=False) for n in ("start_ms", "contact_ms", "pause_ms", "end_ms")],
        sa.Column("approval", sa.Text(), nullable=False),
        sa.Column("provenance", sa.JSON(), nullable=False),
        sa.Column("answer_basis", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("family_id", "version"),
        sa.UniqueConstraint("generation_id", "candidate_index"),
        sa.CheckConstraint("start_ms >= 0 AND start_ms < contact_ms AND contact_ms < pause_ms AND pause_ms < end_ms", name="ck_quiz_bounds"))
    op.create_index("ix_quiz_video_current", "quiz_items", ["video_id", "is_current"])
    op.create_table("quiz_attempts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("quiz_item_id", sa.Text(), sa.ForeignKey("quiz_items.id"), nullable=False),
        sa.Column("user_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("request_id", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("answer", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("elapsed_ms", sa.BigInteger(), nullable=False),
        sa.Column("scored", sa.Boolean(), nullable=False),
        sa.Column("feedback_version", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "request_id"))


def downgrade():
    for name in ("quiz_attempts", "quiz_items", "quiz_generations"):
        op.drop_table(name)
