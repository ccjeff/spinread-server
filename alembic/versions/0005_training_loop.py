"""Player, AI and coach collaboration with versioned plans and retests."""
from alembic import op
import sqlalchemy as sa

revision = "0005_training_loop"
down_revision = "0004_practice"
branch_labels = depends_on = None

def upgrade():
    op.add_column("videos", sa.Column("training_context", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("videos", sa.Column("context_version", sa.Integer(), nullable=False, server_default="1"))
    op.create_table('coach_grants',
        sa.Column('id', sa.Text(), primary_key=True, nullable=False),
        sa.Column('player_id', sa.Text(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('coach_id', sa.Text(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('player_id', 'coach_id'),
    )
    op.create_table('consent_grants',
        sa.Column('id', sa.Text(), primary_key=True, nullable=False),
        sa.Column('video_id', sa.Text(), sa.ForeignKey('videos.id'), nullable=False),
        sa.Column('subject_user_id', sa.Text(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('purpose', sa.Text(), nullable=False),
        sa.Column('granted_to', sa.Text(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('state', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('granted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint('video_id', 'purpose', 'granted_to'),
    )
    op.create_table('audit_events',
        sa.Column('id', sa.Text(), primary_key=True, nullable=False),
        sa.Column('actor_id', sa.Text(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('video_id', sa.Text(), sa.ForeignKey('videos.id'), nullable=True),
        sa.Column('kind', sa.Text(), nullable=False),
        sa.Column('resource_id', sa.Text(), nullable=False),
        sa.Column('details', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table('idempotency_keys',
        sa.Column('id', sa.Text(), primary_key=True, nullable=False),
        sa.Column('user_id', sa.Text(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('key', sa.Text(), nullable=False),
        sa.Column('request_hash', sa.Text(), nullable=False),
        sa.Column('response_json', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'key'),
    )
    op.create_table('training_plans',
        sa.Column('id', sa.Text(), primary_key=True, nullable=False),
        sa.Column('user_id', sa.Text(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('source_report_id', sa.Text(), sa.ForeignKey('analysis_reports.id'), nullable=False),
        sa.Column('state', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table('plan_items',
        sa.Column('id', sa.Text(), primary_key=True, nullable=False),
        sa.Column('plan_id', sa.Text(), sa.ForeignKey('training_plans.id'), nullable=False),
        sa.Column('source_report_id', sa.Text(), sa.ForeignKey('analysis_reports.id'), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('priority', sa.Integer(), nullable=False),
        sa.Column('title', sa.Text(), nullable=False),
        sa.Column('finding_ids', sa.JSON(), nullable=False),
        sa.Column('drill', sa.JSON(), nullable=False),
        sa.Column('retest', sa.JSON(), nullable=False),
        sa.Column('source', sa.Text(), nullable=False),
        sa.Column('locked_by_coach', sa.Boolean(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('player_note', sa.Text(), nullable=False),
        sa.Column('history', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table('plan_item_retests',
        sa.Column('id', sa.Text(), primary_key=True, nullable=False),
        sa.Column('plan_item_id', sa.Text(), sa.ForeignKey('plan_items.id'), nullable=False),
        sa.Column('plan_item_version', sa.Integer(), nullable=False),
        sa.Column('video_id', sa.Text(), sa.ForeignKey('videos.id'), nullable=False),
        sa.Column('report_id', sa.Text(), sa.ForeignKey('analysis_reports.id'), nullable=False),
        sa.Column('result', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table('review_requests',
        sa.Column('id', sa.Text(), primary_key=True, nullable=False),
        sa.Column('video_id', sa.Text(), sa.ForeignKey('videos.id'), nullable=False),
        sa.Column('coach_id', sa.Text(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('timeline_version', sa.Integer(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table('coach_feedback',
        sa.Column('id', sa.Text(), primary_key=True, nullable=False),
        sa.Column('review_request_id', sa.Text(), sa.ForeignKey('review_requests.id'), nullable=False),
        sa.Column('timeline_item_id', sa.Text(), sa.ForeignKey('timeline_items.id'), nullable=True),
        sa.Column('start_ms', sa.BigInteger(), nullable=True),
        sa.Column('end_ms', sa.BigInteger(), nullable=True),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('provenance', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("uq_active_training_plan", "training_plans", ["user_id"], unique=True, postgresql_where=sa.text("state = 'ACTIVE'"), sqlite_where=sa.text("state = 'ACTIVE'"))

def downgrade():
    op.drop_table('coach_feedback')
    op.drop_table('review_requests')
    op.drop_table('plan_item_retests')
    op.drop_table('plan_items')
    op.drop_table('training_plans')
    op.drop_table('idempotency_keys')
    op.drop_table('audit_events')
    op.drop_table('consent_grants')
    op.drop_table('coach_grants')
    op.drop_column("videos", "context_version")
    op.drop_column("videos", "training_context")
