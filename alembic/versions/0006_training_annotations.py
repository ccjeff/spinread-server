"""Persist independent, versioned human training chapters."""
from alembic import op
import sqlalchemy as sa
revision = "0006_training_annotations"
down_revision = "0005_training_loop"
branch_labels = depends_on = None

def upgrade():
    op.create_table("training_annotation_revisions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("video_id", sa.Text(), sa.ForeignKey("videos.id"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("segments", sa.JSON(), nullable=False),
        sa.Column("author_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("video_id", "version"),
    )

def downgrade():
    op.drop_table("training_annotation_revisions")
