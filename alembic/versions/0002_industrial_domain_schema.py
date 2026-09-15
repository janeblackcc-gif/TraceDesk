"""industrial domain schema

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from pgvector.sqlalchemy import Vector

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Frozen initial domain DDL; do not import mutable application metadata.
    op.create_table('file_objects',
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('storage_key', sa.Text(), nullable=False),
    sa.Column('detected_type', sa.String(length=100), nullable=False),
    sa.Column('original_extension', sa.String(length=20), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f('ck_file_objects_valid_sha256')),
    sa.CheckConstraint('size_bytes >= 0', name=op.f('ck_file_objects_nonnegative_size')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_file_objects')),
    sa.UniqueConstraint('sha256', name=op.f('uq_file_objects_sha256')),
    sa.UniqueConstraint('storage_key', name=op.f('uq_file_objects_storage_key'))
    )
    op.create_table('model_profiles',
    sa.Column('provider', sa.String(length=100), nullable=False),
    sa.Column('model_tag', sa.String(length=200), nullable=False),
    sa.Column('model_digest', sa.String(length=100), nullable=False),
    sa.Column('dimension', sa.Integer(), nullable=False),
    sa.Column('input_profile', sa.String(length=100), nullable=False),
    sa.Column('prompt_profile', sa.String(length=100), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('dimension = 1024', name=op.f('ck_model_profiles_embedding_dimension')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_model_profiles')),
    sa.UniqueConstraint('provider', 'model_digest', 'input_profile', name=op.f('uq_model_profiles_provider'))
    )
    op.create_table('users',
    sa.Column('email', postgresql.CITEXT(), nullable=False),
    sa.Column('display_name', sa.String(length=120), nullable=False),
    sa.Column('password_hash', sa.Text(), nullable=True),
    sa.Column('status', sa.Enum('active', 'disabled', name='user_status', native_enum=False, create_constraint=True), server_default='active', nullable=False),
    sa.Column('is_system_admin', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('auth_version', sa.BigInteger(), server_default='1', nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('auth_version > 0', name=op.f('ck_users_positive_auth_version')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email'))
    )
    op.create_table('workspaces',
    sa.Column('name', sa.String(length=120), nullable=False),
    sa.Column('slug', sa.String(length=120), nullable=False),
    sa.Column('permission_epoch', sa.BigInteger(), server_default='1', nullable=False),
    sa.Column('migration_key', sa.String(length=200), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_workspaces')),
    sa.UniqueConstraint('migration_key', name=op.f('uq_workspaces_migration_key')),
    sa.UniqueConstraint('slug', name=op.f('uq_workspaces_slug'))
    )
    op.create_table('knowledge_bases',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('slug', sa.String(length=120), nullable=False),
    sa.Column('active_index_generation_id', sa.Uuid(), nullable=True),
    sa.Column('data_epoch', sa.BigInteger(), server_default='1', nullable=False),
    sa.Column('permission_epoch', sa.BigInteger(), server_default='1', nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('migration_key', sa.String(length=200), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('data_epoch > 0 AND permission_epoch > 0', name=op.f('ck_knowledge_bases_positive_epochs')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_knowledge_bases_workspace_id_workspaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_knowledge_bases')),
    sa.UniqueConstraint('id', 'workspace_id', name=op.f('uq_knowledge_bases_id')),
    sa.UniqueConstraint('migration_key', name=op.f('uq_knowledge_bases_migration_key')),
    sa.UniqueConstraint('workspace_id', 'slug', name=op.f('uq_knowledge_bases_workspace_id'))
    )
    op.create_index('ix_knowledge_bases_active_index_generation_id_id', 'knowledge_bases', ['active_index_generation_id', 'id'], unique=False)
    op.create_table('sessions',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.LargeBinary(), nullable=False),
    sa.Column('csrf_hash', sa.LargeBinary(), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('auth_version_at_issue', sa.BigInteger(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('octet_length(token_hash) = 32', name=op.f('ck_sessions_token_sha256')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_sessions_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_sessions_token_hash'))
    )
    op.create_index('ix_sessions_user_id', 'sessions', ['user_id'], unique=False)
    op.create_table('workspace_members',
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.Enum('admin', 'member', name='workspace_role', native_enum=False, create_constraint=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_workspace_members_user_id_users')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_workspace_members_workspace_id_workspaces')),
    sa.PrimaryKeyConstraint('workspace_id', 'user_id', name=op.f('pk_workspace_members'))
    )
    op.create_index('ix_workspace_members_user_id', 'workspace_members', ['user_id'], unique=False)
    op.create_table('audit_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('occurred_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('actor_user_id', sa.Uuid(), nullable=True),
    sa.Column('workspace_id', sa.Uuid(), nullable=True),
    sa.Column('kb_id', sa.Uuid(), nullable=True),
    sa.Column('action', sa.String(length=100), nullable=False),
    sa.Column('resource_type', sa.String(length=100), nullable=False),
    sa.Column('resource_id', sa.String(length=200), nullable=True),
    sa.Column('outcome', sa.String(length=50), nullable=False),
    sa.Column('request_id', sa.String(length=100), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_audit_events_actor_user_id_users')),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], name=op.f('fk_audit_events_kb_id_knowledge_bases')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_audit_events_workspace_id_workspaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events'))
    )
    op.create_index('ix_audit_events_actor_user_id', 'audit_events', ['actor_user_id'], unique=False)
    op.create_index('ix_audit_events_kb_id', 'audit_events', ['kb_id'], unique=False)
    op.create_index('ix_audit_workspace_time', 'audit_events', ['workspace_id', 'occurred_at'], unique=False)
    op.create_table('conversations',
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('kb_id', sa.Uuid(), nullable=False),
    sa.Column('last_activity_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], name=op.f('fk_conversations_kb_id_knowledge_bases')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_conversations_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_conversations'))
    )
    op.create_index('ix_conversations_kb_id', 'conversations', ['kb_id'], unique=False)
    op.create_index('ix_conversations_user_id', 'conversations', ['user_id'], unique=False)
    op.create_table('eval_datasets',
    sa.Column('kb_id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('version', sa.String(length=100), nullable=False),
    sa.Column('frozen_hash', sa.String(length=64), nullable=False),
    sa.Column('provenance', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.Enum('dev', 'holdout', name='dataset_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], name=op.f('fk_eval_datasets_kb_id_knowledge_bases')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_eval_datasets')),
    sa.UniqueConstraint('kb_id', 'name', 'version', name=op.f('uq_eval_datasets_kb_id'))
    )
    op.create_table('index_generations',
    sa.Column('kb_id', sa.Uuid(), nullable=False),
    sa.Column('model_profile_id', sa.Uuid(), nullable=False),
    sa.Column('retrieval_config_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.Enum('building', 'ready', 'active', 'superseded', 'failed', name='generation_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('corpus_manifest_hash', sa.String(length=64), nullable=False),
    sa.Column('chunk_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint('chunk_count >= 0', name=op.f('ck_index_generations_nonnegative_chunks')),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], name=op.f('fk_index_generations_kb_id_knowledge_bases')),
    sa.ForeignKeyConstraint(['model_profile_id'], ['model_profiles.id'], name=op.f('fk_index_generations_model_profile_id_model_profiles')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_index_generations')),
    sa.UniqueConstraint('id', 'kb_id', name=op.f('uq_index_generations_id'))
    )
    op.create_index('ix_index_generations_model_profile_id', 'index_generations', ['model_profile_id'], unique=False)
    op.create_index('uq_generation_active_kb', 'index_generations', ['kb_id'], unique=True, postgresql_where=sa.text("status = 'active'"))
    op.create_table('kb_members',
    sa.Column('kb_id', sa.Uuid(), nullable=False),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('role', sa.Enum('editor', 'viewer', name='kb_role', native_enum=False, create_constraint=True), nullable=False),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], name=op.f('fk_kb_members_kb_id_knowledge_bases')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_kb_members_user_id_users')),
    sa.PrimaryKeyConstraint('kb_id', 'user_id', name=op.f('pk_kb_members'))
    )
    op.create_index('ix_kb_members_user_id', 'kb_members', ['user_id'], unique=False)
    op.create_table('legacy_aliases',
    sa.Column('kind', sa.Enum('document', 'chunk', name='alias_kind', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('legacy_id', sa.String(length=100), nullable=False),
    sa.Column('kb_id', sa.Uuid(), nullable=False),
    sa.Column('target_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('text_sha256', sa.String(length=64), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], name=op.f('fk_legacy_aliases_kb_id_knowledge_bases')),
    sa.PrimaryKeyConstraint('kind', 'legacy_id', name=op.f('pk_legacy_aliases'))
    )
    op.create_index('ix_legacy_aliases_kb_id', 'legacy_aliases', ['kb_id'], unique=False)
    op.create_table('logical_documents',
    sa.Column('kb_id', sa.Uuid(), nullable=False),
    sa.Column('display_name', sa.String(length=255), nullable=False),
    sa.Column('normalized_key', sa.String(length=255), nullable=False),
    sa.Column('desired_revision_id', sa.Uuid(), nullable=True),
    sa.Column('active_revision_id', sa.Uuid(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_logical_documents_created_by_users')),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], name=op.f('fk_logical_documents_kb_id_knowledge_bases')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_logical_documents')),
    sa.UniqueConstraint('id', 'kb_id', name=op.f('uq_logical_documents_id'))
    )
    op.create_index('ix_logical_documents_active_revision_id_id', 'logical_documents', ['active_revision_id', 'id'], unique=False)
    op.create_index('ix_logical_documents_created_by', 'logical_documents', ['created_by'], unique=False)
    op.create_index('ix_logical_documents_desired_revision_id_id', 'logical_documents', ['desired_revision_id', 'id'], unique=False)
    op.create_index('uq_live_document_name', 'logical_documents', ['kb_id', 'normalized_key'], unique=True, postgresql_where=sa.text('deleted_at IS NULL'))
    op.create_table('document_revisions',
    sa.Column('document_id', sa.Uuid(), nullable=False),
    sa.Column('revision_no', sa.Integer(), nullable=False),
    sa.Column('file_object_id', sa.Uuid(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('status', sa.Enum('uploaded', 'parsing', 'parsed', 'indexing', 'ready', 'failed', 'superseded', 'deleted', 'quarantined', name='revision_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('parser_profile', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('activated_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('legacy_doc_id', sa.String(length=64), nullable=True),
    sa.Column('migration_key', sa.String(length=200), nullable=True),
    sa.Column('source_version_label', sa.String(length=60), nullable=True),
    sa.Column('source_file_available', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f('ck_document_revisions_valid_sha256')),
    sa.CheckConstraint('revision_no > 0', name=op.f('ck_document_revisions_positive_revision')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_document_revisions_created_by_users')),
    sa.ForeignKeyConstraint(['document_id'], ['logical_documents.id'], name=op.f('fk_document_revisions_document_id_logical_documents')),
    sa.ForeignKeyConstraint(['file_object_id'], ['file_objects.id'], name=op.f('fk_document_revisions_file_object_id_file_objects')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_document_revisions')),
    sa.UniqueConstraint('document_id', 'revision_no', name=op.f('uq_document_revisions_document_id')),
    sa.UniqueConstraint('id', 'document_id', name=op.f('uq_document_revisions_id')),
    sa.UniqueConstraint('legacy_doc_id', name=op.f('uq_document_revisions_legacy_doc_id')),
    sa.UniqueConstraint('migration_key', name=op.f('uq_document_revisions_migration_key'))
    )
    op.create_index('ix_document_revisions_created_by', 'document_revisions', ['created_by'], unique=False)
    op.create_index('ix_document_revisions_file_object_id', 'document_revisions', ['file_object_id'], unique=False)
    op.create_table('eval_cases',
    sa.Column('dataset_id', sa.Uuid(), nullable=False),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('scope', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('task_type', sa.String(length=100), nullable=False),
    sa.Column('answerability', sa.Boolean(), nullable=False),
    sa.Column('required_facts', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('gold_passages', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('severity', sa.String(length=50), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['dataset_id'], ['eval_datasets.id'], name=op.f('fk_eval_cases_dataset_id_eval_datasets')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_eval_cases'))
    )
    op.create_index('ix_eval_cases_dataset_id', 'eval_cases', ['dataset_id'], unique=False)
    op.create_table('eval_runs',
    sa.Column('dataset_id', sa.Uuid(), nullable=False),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('code_commit', sa.String(length=64), nullable=False),
    sa.Column('dataset_hash', sa.String(length=64), nullable=False),
    sa.Column('corpus_manifest', sa.String(length=64), nullable=False),
    sa.Column('model_digest', sa.String(length=100), nullable=False),
    sa.Column('prompt_config_hash', sa.String(length=64), nullable=False),
    sa.Column('retrieval_config_hash', sa.String(length=64), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_eval_runs_created_by_users')),
    sa.ForeignKeyConstraint(['dataset_id'], ['eval_datasets.id'], name=op.f('fk_eval_runs_dataset_id_eval_datasets')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_eval_runs'))
    )
    op.create_index('ix_eval_runs_created_by', 'eval_runs', ['created_by'], unique=False)
    op.create_index('ix_eval_runs_dataset_id', 'eval_runs', ['dataset_id'], unique=False)
    op.create_table('eval_results',
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('case_id', sa.Uuid(), nullable=False),
    sa.Column('retrieved_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('generation_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('automatic_metrics', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('human_scores', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('reviewer_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('dispute_status', sa.String(length=50), nullable=False),
    sa.ForeignKeyConstraint(['case_id'], ['eval_cases.id'], name=op.f('fk_eval_results_case_id_eval_cases')),
    sa.ForeignKeyConstraint(['run_id'], ['eval_runs.id'], name=op.f('fk_eval_results_run_id_eval_runs')),
    sa.PrimaryKeyConstraint('run_id', 'case_id', name=op.f('pk_eval_results'))
    )
    op.create_index('ix_eval_results_case_id', 'eval_results', ['case_id'], unique=False)
    op.create_table('generation_revisions',
    sa.Column('generation_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['generation_id'], ['index_generations.id'], name=op.f('fk_generation_revisions_generation_id_index_generations')),
    sa.ForeignKeyConstraint(['revision_id'], ['document_revisions.id'], name=op.f('fk_generation_revisions_revision_id_document_revisions')),
    sa.PrimaryKeyConstraint('generation_id', 'revision_id', name=op.f('pk_generation_revisions'))
    )
    op.create_index('ix_generation_revisions_revision_id', 'generation_revisions', ['revision_id'], unique=False)
    op.create_table('jobs',
    sa.Column('type', sa.Enum('parse', 'index', 'query', 'gc', 'eval', 'reindex', name='job_type', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('kb_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=True),
    sa.Column('generation_id', sa.Uuid(), nullable=True),
    sa.Column('state', sa.Enum('queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled', 'superseded', name='job_state', native_enum=False, create_constraint=True), server_default='queued', nullable=False),
    sa.Column('priority', sa.Integer(), server_default='0', nullable=False),
    sa.Column('idempotency_key', sa.String(length=200), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('payload_hash', sa.String(length=64), nullable=False),
    sa.Column('attempt_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('max_attempts', sa.Integer(), server_default='3', nullable=False),
    sa.Column('available_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('lease_owner', sa.String(length=200), nullable=True),
    sa.Column('lease_expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_requested_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('result', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('error_code', sa.String(length=100), nullable=True),
    sa.Column('created_by', sa.Uuid(), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("state != 'running' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)", name=op.f('ck_jobs_running_lease')),
    sa.CheckConstraint('attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts', name=op.f('ck_jobs_attempt_budget')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_jobs_created_by_users')),
    sa.ForeignKeyConstraint(['generation_id', 'kb_id'], ['index_generations.id', 'index_generations.kb_id'], name=op.f('fk_jobs_generation_id_index_generations')),
    sa.ForeignKeyConstraint(['kb_id', 'workspace_id'], ['knowledge_bases.id', 'knowledge_bases.workspace_id'], name=op.f('fk_jobs_kb_id_knowledge_bases')),
    sa.ForeignKeyConstraint(['revision_id'], ['document_revisions.id'], name=op.f('fk_jobs_revision_id_document_revisions')),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], name=op.f('fk_jobs_workspace_id_workspaces')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_jobs')),
    sa.UniqueConstraint('type', 'idempotency_key', name=op.f('uq_jobs_type'))
    )
    op.create_index('ix_jobs_claim', 'jobs', ['state', 'available_at', 'priority', 'created_at'], unique=False)
    op.create_index('ix_jobs_created_by', 'jobs', ['created_by'], unique=False)
    op.create_index('ix_jobs_expired', 'jobs', ['lease_expires_at'], unique=False, postgresql_where=sa.text("state = 'running'"))
    op.create_index('ix_jobs_generation_id_kb_id', 'jobs', ['generation_id', 'kb_id'], unique=False)
    op.create_index('ix_jobs_kb_id_workspace_id', 'jobs', ['kb_id', 'workspace_id'], unique=False)
    op.create_index('ix_jobs_revision_id', 'jobs', ['revision_id'], unique=False)
    op.create_index('ix_jobs_workspace_id', 'jobs', ['workspace_id'], unique=False)
    op.create_table('migration_checkpoints',
    sa.Column('source_db_hash', sa.String(length=64), nullable=False),
    sa.Column('legacy_doc_id', sa.String(length=64), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('completed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['revision_id'], ['document_revisions.id'], name=op.f('fk_migration_checkpoints_revision_id_document_revisions')),
    sa.PrimaryKeyConstraint('source_db_hash', 'legacy_doc_id', name=op.f('pk_migration_checkpoints'))
    )
    op.create_index('ix_migration_checkpoints_revision_id', 'migration_checkpoints', ['revision_id'], unique=False)
    op.create_table('parse_runs',
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('parser_name', sa.String(length=100), nullable=False),
    sa.Column('parser_version', sa.String(length=100), nullable=False),
    sa.Column('config_hash', sa.String(length=64), nullable=False),
    sa.Column('status', sa.Enum('running', 'succeeded', 'failed', 'cancelled', name='parse_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error_code', sa.String(length=100), nullable=True),
    sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['revision_id'], ['document_revisions.id'], name=op.f('fk_parse_runs_revision_id_document_revisions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_parse_runs')),
    sa.UniqueConstraint('id', 'revision_id', name=op.f('uq_parse_runs_id'))
    )
    op.create_index('ix_parse_runs_revision_id', 'parse_runs', ['revision_id'], unique=False)
    op.create_table('chunks',
    sa.Column('parse_run_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('page_start', sa.Integer(), nullable=False),
    sa.Column('page_end', sa.Integer(), nullable=False),
    sa.Column('line_start', sa.Integer(), nullable=False),
    sa.Column('line_end', sa.Integer(), nullable=False),
    sa.Column('heading', sa.Text(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('text_sha256', sa.String(length=64), nullable=False),
    sa.Column('token_count', sa.Integer(), nullable=True),
    sa.Column('legacy_chunk_id', sa.String(length=100), nullable=True),
    sa.Column('search_tsv', postgresql.TSVECTOR(), server_default=sa.text("''::tsvector"), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('ordinal >= 0 AND page_start > 0 AND page_end >= page_start AND line_start > 0 AND line_end >= line_start', name=op.f('ck_chunks_valid_position')),
    sa.ForeignKeyConstraint(['parse_run_id', 'revision_id'], ['parse_runs.id', 'parse_runs.revision_id'], name=op.f('fk_chunks_parse_run_id_parse_runs')),
    sa.ForeignKeyConstraint(['revision_id'], ['document_revisions.id'], name=op.f('fk_chunks_revision_id_document_revisions')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_chunks')),
    sa.UniqueConstraint('id', 'revision_id', name=op.f('uq_chunks_id')),
    sa.UniqueConstraint('legacy_chunk_id', name=op.f('uq_chunks_legacy_chunk_id')),
    sa.UniqueConstraint('parse_run_id', 'ordinal', name=op.f('uq_chunks_parse_run_id'))
    )
    op.create_index('ix_chunks_parse_run_id_revision_id', 'chunks', ['parse_run_id', 'revision_id'], unique=False)
    op.create_index('ix_chunks_revision_ordinal', 'chunks', ['revision_id', 'ordinal'], unique=False)
    op.create_index('ix_chunks_search_tsv', 'chunks', ['search_tsv'], unique=False, postgresql_using='gin')
    op.create_table('job_attempts',
    sa.Column('job_id', sa.Uuid(), nullable=False),
    sa.Column('attempt_no', sa.Integer(), nullable=False),
    sa.Column('worker_id', sa.String(length=200), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('outcome', sa.String(length=100), nullable=True),
    sa.Column('error_code', sa.String(length=100), nullable=True),
    sa.Column('metrics', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_job_attempts_job_id_jobs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_job_attempts')),
    sa.UniqueConstraint('job_id', 'attempt_no', name=op.f('uq_job_attempts_job_id'))
    )
    op.create_table('parsed_pages',
    sa.Column('parse_run_id', sa.Uuid(), nullable=False),
    sa.Column('page_no', sa.Integer(), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('text_sha256', sa.String(length=64), nullable=False),
    sa.Column('metadata', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.CheckConstraint('page_no > 0', name=op.f('ck_parsed_pages_positive_page')),
    sa.ForeignKeyConstraint(['parse_run_id'], ['parse_runs.id'], name=op.f('fk_parsed_pages_parse_run_id_parse_runs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_parsed_pages')),
    sa.UniqueConstraint('parse_run_id', 'page_no', name=op.f('uq_parsed_pages_parse_run_id'))
    )
    op.create_table('query_runs',
    sa.Column('conversation_id', sa.Uuid(), nullable=True),
    sa.Column('user_id', sa.Uuid(), nullable=False),
    sa.Column('kb_id', sa.Uuid(), nullable=False),
    sa.Column('generation_id', sa.Uuid(), nullable=True),
    sa.Column('model_profile_id', sa.Uuid(), nullable=True),
    sa.Column('job_id', sa.Uuid(), nullable=True),
    sa.Column('question', sa.Text(), nullable=False),
    sa.Column('scope', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('retrieval_config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('status', sa.Enum('queued', 'running', 'succeeded', 'failed', 'partial', 'cancelled', 'superseded', name='query_status', native_enum=False, create_constraint=True), nullable=False),
    sa.Column('latencies', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.Column('auth_version', sa.BigInteger(), nullable=False),
    sa.Column('permission_epoch', sa.BigInteger(), nullable=False),
    sa.Column('workspace_permission_epoch', sa.BigInteger(), nullable=False),
    sa.Column('data_epoch', sa.BigInteger(), nullable=False),
    sa.Column('response', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversations.id'], name=op.f('fk_query_runs_conversation_id_conversations')),
    sa.ForeignKeyConstraint(['generation_id', 'kb_id'], ['index_generations.id', 'index_generations.kb_id'], name=op.f('fk_query_runs_generation_id_index_generations')),
    sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], name=op.f('fk_query_runs_job_id_jobs')),
    sa.ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id'], name=op.f('fk_query_runs_kb_id_knowledge_bases')),
    sa.ForeignKeyConstraint(['model_profile_id'], ['model_profiles.id'], name=op.f('fk_query_runs_model_profile_id_model_profiles')),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_query_runs_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_query_runs'))
    )
    op.create_index('ix_query_runs_conversation_id', 'query_runs', ['conversation_id'], unique=False)
    op.create_index('ix_query_runs_generation_id_kb_id', 'query_runs', ['generation_id', 'kb_id'], unique=False)
    op.create_index('ix_query_runs_job_id', 'query_runs', ['job_id'], unique=False)
    op.create_index('ix_query_runs_kb_id', 'query_runs', ['kb_id'], unique=False)
    op.create_index('ix_query_runs_model_profile_id', 'query_runs', ['model_profile_id'], unique=False)
    op.create_index('ix_query_runs_user_created', 'query_runs', ['user_id', 'created_at'], unique=False)
    op.create_table('chunk_embeddings',
    sa.Column('generation_id', sa.Uuid(), nullable=False),
    sa.Column('chunk_id', sa.Uuid(), nullable=False),
    sa.Column('revision_id', sa.Uuid(), nullable=False),
    sa.Column('embedding', Vector(1024), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['chunk_id', 'revision_id'], ['chunks.id', 'chunks.revision_id'], name=op.f('fk_chunk_embeddings_chunk_id_chunks')),
    sa.ForeignKeyConstraint(['generation_id', 'revision_id'], ['generation_revisions.generation_id', 'generation_revisions.revision_id'], name=op.f('fk_chunk_embeddings_generation_id_generation_revisions')),
    sa.PrimaryKeyConstraint('generation_id', 'chunk_id', name=op.f('pk_chunk_embeddings'))
    )
    op.create_index('ix_chunk_embeddings_chunk_id_revision_id', 'chunk_embeddings', ['chunk_id', 'revision_id'], unique=False)
    op.create_index('ix_chunk_embeddings_generation_id_revision_id', 'chunk_embeddings', ['generation_id', 'revision_id'], unique=False)
    op.create_table('query_sources',
    sa.Column('query_run_id', sa.Uuid(), nullable=False),
    sa.Column('chunk_id', sa.Uuid(), nullable=False),
    sa.Column('rank', sa.Integer(), nullable=False),
    sa.Column('channel', sa.String(length=50), nullable=False),
    sa.Column('score', sa.Float(), nullable=False),
    sa.Column('context_origin', sa.String(length=100), nullable=False),
    sa.ForeignKeyConstraint(['chunk_id'], ['chunks.id'], name=op.f('fk_query_sources_chunk_id_chunks')),
    sa.ForeignKeyConstraint(['query_run_id'], ['query_runs.id'], name=op.f('fk_query_sources_query_run_id_query_runs')),
    sa.PrimaryKeyConstraint('query_run_id', 'chunk_id', name=op.f('pk_query_sources'))
    )
    op.create_index('ix_query_sources_chunk_id', 'query_sources', ['chunk_id'], unique=False)
    op.create_table('trace_records',
    sa.Column('query_run_id', sa.Uuid(), nullable=False),
    sa.Column('details', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['query_run_id'], ['query_runs.id'], name=op.f('fk_trace_records_query_run_id_query_runs')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_trace_records'))
    )
    op.create_index('ix_trace_records_query_run_id', 'trace_records', ['query_run_id'], unique=False)
    op.create_foreign_key('fk_kb_active_generation_scope', 'knowledge_bases', 'index_generations',
                          ['active_index_generation_id', 'id'], ['id', 'kb_id'])
    op.create_foreign_key('fk_document_desired_scope', 'logical_documents', 'document_revisions',
                          ['desired_revision_id', 'id'], ['id', 'document_id'])
    op.create_foreign_key('fk_document_active_scope', 'logical_documents', 'document_revisions',
                          ['active_revision_id', 'id'], ['id', 'document_id'])


def downgrade() -> None:
    raise RuntimeError('Domain schema downgrade is destructive; restore a verified backup into a new database')
