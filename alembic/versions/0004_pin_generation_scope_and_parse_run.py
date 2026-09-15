"""pin generation scope and parse run

Revision ID: 0004
Revises: 0003
"""
from alembic import op
import sqlalchemy as sa


revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add nullable fields, backfill from existing relationships, then constrain.
    op.add_column('chunk_embeddings', sa.Column('parse_run_id', sa.Uuid(), nullable=True))
    op.add_column('generation_revisions', sa.Column('kb_id', sa.Uuid(), nullable=True))
    op.add_column('generation_revisions', sa.Column('document_id', sa.Uuid(), nullable=True))
    op.add_column('generation_revisions', sa.Column('parse_run_id', sa.Uuid(), nullable=True))
    op.execute('''UPDATE chunk_embeddings e SET parse_run_id=c.parse_run_id
                  FROM chunks c WHERE c.id=e.chunk_id AND c.revision_id=e.revision_id''')
    op.execute('''UPDATE generation_revisions g SET document_id=r.document_id,kb_id=d.kb_id
                  FROM document_revisions r JOIN logical_documents d ON d.id=r.document_id
                  WHERE r.id=g.revision_id''')
    op.execute('''UPDATE generation_revisions g SET parse_run_id=(
                    SELECT (array_agg(DISTINCT e.parse_run_id))[1] FROM chunk_embeddings e
                    WHERE e.generation_id=g.generation_id AND e.revision_id=g.revision_id
                    HAVING count(DISTINCT e.parse_run_id)=1)''')
    # Empty generations can only inherit an unambiguous successful parse.
    op.execute('''UPDATE generation_revisions g SET parse_run_id=(
                    SELECT (array_agg(p.id))[1] FROM parse_runs p
                    WHERE p.revision_id=g.revision_id AND p.status='succeeded' HAVING count(*)=1)
                  WHERE g.parse_run_id IS NULL AND NOT EXISTS (
                    SELECT 1 FROM chunk_embeddings e WHERE e.generation_id=g.generation_id AND e.revision_id=g.revision_id)''')
    for table, columns in [('generation_revisions', ['kb_id', 'document_id', 'parse_run_id']),
                           ('chunk_embeddings', ['parse_run_id'])]:
        for column in columns:
            op.alter_column(table, column, nullable=False)
    op.drop_index(op.f('ix_chunk_embeddings_chunk_id_revision_id'), table_name='chunk_embeddings')
    op.drop_index(op.f('ix_chunk_embeddings_generation_id_revision_id'), table_name='chunk_embeddings')
    op.create_index('ix_chunk_embeddings_chunk_id_revision_id_parse_run_id', 'chunk_embeddings', ['chunk_id', 'revision_id', 'parse_run_id'], unique=False)
    op.create_index('ix_chunk_embeddings_generation_id_revision_id_parse_run_id', 'chunk_embeddings', ['generation_id', 'revision_id', 'parse_run_id'], unique=False)
    op.drop_constraint(op.f('fk_chunk_embeddings_chunk_id_chunks'), 'chunk_embeddings', type_='foreignkey')
    op.drop_constraint(op.f('fk_chunk_embeddings_generation_id_generation_revisions'), 'chunk_embeddings', type_='foreignkey')
    op.create_unique_constraint('uq_chunks_id_revision_parse', 'chunks', ['id', 'revision_id', 'parse_run_id'])
    op.drop_index(op.f('ix_generation_revisions_revision_id'), table_name='generation_revisions')
    op.create_index('ix_generation_revisions_document_id_kb_id', 'generation_revisions', ['document_id', 'kb_id'], unique=False)
    op.create_index('ix_generation_revisions_generation_id_kb_id', 'generation_revisions', ['generation_id', 'kb_id'], unique=False)
    op.create_index('ix_generation_revisions_parse_run_id_revision_id', 'generation_revisions', ['parse_run_id', 'revision_id'], unique=False)
    op.create_index('ix_generation_revisions_revision_id_document_id', 'generation_revisions', ['revision_id', 'document_id'], unique=False)
    op.create_unique_constraint(op.f('uq_generation_revisions_generation_id'), 'generation_revisions', ['generation_id', 'revision_id', 'parse_run_id'])
    op.drop_constraint(op.f('fk_generation_revisions_revision_id_document_revisions'), 'generation_revisions', type_='foreignkey')
    op.drop_constraint(op.f('fk_generation_revisions_generation_id_index_generations'), 'generation_revisions', type_='foreignkey')
    op.create_foreign_key(op.f('fk_generation_revisions_revision_id_document_revisions'), 'generation_revisions', 'document_revisions', ['revision_id', 'document_id'], ['id', 'document_id'])
    op.create_foreign_key(op.f('fk_generation_revisions_generation_id_index_generations'), 'generation_revisions', 'index_generations', ['generation_id', 'kb_id'], ['id', 'kb_id'])
    op.create_foreign_key(op.f('fk_generation_revisions_document_id_logical_documents'), 'generation_revisions', 'logical_documents', ['document_id', 'kb_id'], ['id', 'kb_id'])
    op.create_foreign_key(op.f('fk_generation_revisions_parse_run_id_parse_runs'), 'generation_revisions', 'parse_runs', ['parse_run_id', 'revision_id'], ['id', 'revision_id'])
    op.create_foreign_key(op.f('fk_chunk_embeddings_generation_id_generation_revisions'), 'chunk_embeddings', 'generation_revisions', ['generation_id', 'revision_id', 'parse_run_id'], ['generation_id', 'revision_id', 'parse_run_id'])
    op.create_foreign_key(op.f('fk_chunk_embeddings_chunk_id_chunks'), 'chunk_embeddings', 'chunks', ['chunk_id', 'revision_id', 'parse_run_id'], ['id', 'revision_id', 'parse_run_id'])


def downgrade() -> None:
    raise RuntimeError('Do not weaken scope constraints; restore a verified backup')
