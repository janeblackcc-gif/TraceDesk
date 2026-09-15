"""T-032: recoverable document deletion and durable object collection outbox."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('file_objects', sa.Column('gc_pending_at', sa.DateTime(timezone=True)))
    op.add_column('document_revisions', sa.Column('purged_at', sa.DateTime(timezone=True)))
    op.alter_column('document_revisions', 'file_object_id', existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint('purged_object', 'document_revisions',
        "file_object_id IS NOT NULL OR (status = 'deleted' AND purged_at IS NOT NULL)")
    op.create_table('document_deletions',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('document_id', sa.Uuid(), sa.ForeignKey('logical_documents.id'), nullable=False),
        sa.Column('deleted_by', sa.Uuid(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('restore_before', sa.DateTime(timezone=True), nullable=False),
        sa.Column('restored_at', sa.DateTime(timezone=True)),
        sa.Column('purged_at', sa.DateTime(timezone=True)),
        sa.Column('snapshot', JSONB(), nullable=False),
        sa.CheckConstraint('restore_before > deleted_at', name='positive_restore_window'),
        sa.CheckConstraint('restored_at IS NULL OR purged_at IS NULL', name='exclusive_delete_outcome'))
    op.create_index('uq_document_current_deletion', 'document_deletions', ['document_id'], unique=True,
                    postgresql_where=sa.text('restored_at IS NULL'))
    op.create_index('ix_document_deletions_deleted_by', 'document_deletions', ['deleted_by'])


def downgrade() -> None:
    raise RuntimeError('Deletion tombstones are durable; restore a verified backup')
