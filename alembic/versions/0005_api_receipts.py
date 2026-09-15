"""T-031: durable request receipts for file upload idempotency."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('api_receipts',
        sa.Column('user_id', sa.Uuid(), sa.ForeignKey('users.id'), primary_key=True),
        sa.Column('operation', sa.String(200), primary_key=True),
        sa.Column('key', sa.String(200), primary_key=True),
        sa.Column('payload_hash', sa.String(64), nullable=False),
        sa.Column('result', JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))


def downgrade() -> None:
    raise RuntimeError('Keep request receipts across rollback; restore a verified backup')
