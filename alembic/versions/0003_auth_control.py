"""T-020/021: one-time initialization and database-backed login throttling."""
from alembic import op
import sqlalchemy as sa

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('bootstrap_state',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('consumed_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint('id = 1', name='singleton'))
    op.create_table('auth_rate_limits',
        sa.Column('bucket_key', sa.String(64), primary_key=True),
        sa.Column('window_started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('hits', sa.Integer(), nullable=False))
    op.create_index('ix_auth_rate_limits_window', 'auth_rate_limits', ['window_started_at'])


def downgrade() -> None:
    raise RuntimeError('Auth state must not be reset; restore a verified backup')
