"""Independent visual assessments with immutable source snapshots."""
from alembic import op
import sqlalchemy as sa
revision = '0007_chapter_analyses'
down_revision = '0006_training_annotations'
branch_labels = depends_on = None


def upgrade():
    op.create_table('chapter_analyses',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('video_id', sa.Text(), sa.ForeignKey('videos.id'), nullable=False),
        sa.Column('author_id', sa.Text(), sa.ForeignKey('users.id'), nullable=False),
        *[sa.Column(name, sa.Text(), nullable=False) for name in ('chapter_id', 'status', 'request_key', 'request_hash', 'cache_hash')],
        sa.Column('start_ms', sa.BigInteger(), nullable=False),
        sa.Column('end_ms', sa.BigInteger(), nullable=False),
        sa.Column('budget_micro_usd', sa.Integer(), nullable=False),
        *[sa.Column(name, sa.JSON(), nullable=False) for name in ('snapshot', 'manifest', 'coverage', 'usage')],
        sa.Column('result', sa.JSON(), nullable=True), sa.Column('error', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('video_id', 'author_id', 'request_key'),
        sa.CheckConstraint('end_ms > start_ms', name='ck_chapter_analysis_range'),
    )
    op.create_index('ix_chapter_analyses_video_created', 'chapter_analyses', ['video_id', 'created_at'])


def downgrade():
    op.drop_table('chapter_analyses')
