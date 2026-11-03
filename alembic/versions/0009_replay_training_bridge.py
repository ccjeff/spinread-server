"""Add immutable coach reference answers and optional replay enrichment jobs.

Existing video, quiz, plan and retest rows are retained unchanged.
"""
from alembic import op
import sqlalchemy as sa

revision = '0009_replay_training_bridge'
down_revision = '0008_replay_reviews'
branch_labels = depends_on = None


def upgrade():
    op.create_table('quiz_reference_answers',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('quiz_item_id', sa.Text(), sa.ForeignKey('quiz_items.id'), nullable=False),
        sa.Column('review_request_id', sa.Text(), sa.ForeignKey('review_requests.id'), nullable=False),
        sa.Column('coach_id', sa.Text(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('answer', sa.JSON(), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('diagnosis', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('quiz_item_id', 'coach_id', 'version'))
    with op.batch_alter_table('plan_items') as batch:
        batch.add_column(sa.Column('quiz_reference_answer_id', sa.Text(), nullable=True))
        batch.create_foreign_key('fk_plan_item_quiz_reference', 'quiz_reference_answers', ['quiz_reference_answer_id'], ['id'])
    op.create_table('replay_analyses',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('video_id', sa.Text(), sa.ForeignKey('videos.id'), nullable=False),
        sa.Column('timeline_id', sa.Text(), sa.ForeignKey('timelines.id'), nullable=False),
        sa.Column('timeline_version', sa.Integer(), nullable=False),
        sa.Column('engine', sa.Text(), nullable=False),
        sa.Column('state', sa.Text(), nullable=False),
        sa.Column('artifact_id', sa.Text(), sa.ForeignKey('artifacts.id'), nullable=True),
        sa.Column('options', sa.JSON(), nullable=False),
        sa.Column('error_code', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))


def downgrade():
    op.drop_table('replay_analyses')
    with op.batch_alter_table('plan_items') as batch:
        batch.drop_constraint('fk_plan_item_quiz_reference', type_='foreignkey')
        batch.drop_column('quiz_reference_answer_id')
    op.drop_table('quiz_reference_answers')
