"""Versioned, private table calibration and landing confirmations."""
from alembic import op
import sqlalchemy as sa
revision = '0008_replay_reviews'
down_revision = '0007_chapter_analyses'
branch_labels = depends_on = None


def upgrade():
    op.create_table('replay_reviews',
        sa.Column('video_id', sa.Text(), sa.ForeignKey('videos.id'), primary_key=True),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('timeline_version', sa.Integer(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('reviewer_id', sa.Text(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table('replay_reviews')
