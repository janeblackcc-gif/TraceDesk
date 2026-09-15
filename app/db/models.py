"""Industrial domain schema. Migration revisions freeze DDL independently."""
from __future__ import annotations

from datetime import datetime
from typing import TypeAlias
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (BigInteger, Boolean, CheckConstraint, DateTime, Enum, ForeignKey,
                        ForeignKeyConstraint, Index, Integer, LargeBinary, String, Text,
                        UniqueConstraint, func, text as sql_text)
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

JsonValue: TypeAlias = str | int | float | bool | None | list['JsonValue'] | dict[str, 'JsonValue']


def states(name: str, *values: str) -> Enum:
    return Enum(*values, name=name, native_enum=False, create_constraint=True, validate_strings=True)


class Identified:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class Created:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Identified, Created, Base):
    __tablename__ = 'users'
    email: Mapped[str] = mapped_column(CITEXT(), unique=True)
    display_name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(states('user_status', 'active', 'disabled'), server_default='active')
    is_system_admin: Mapped[bool] = mapped_column(Boolean, server_default=sql_text('false'))
    auth_version: Mapped[int] = mapped_column(BigInteger, server_default='1')
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint('auth_version > 0', name='positive_auth_version'),)


class UserSession(Identified, Created, Base):
    __tablename__ = 'sessions'
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    csrf_hash: Mapped[bytes] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    auth_version_at_issue: Mapped[int] = mapped_column(BigInteger)
    __table_args__ = (CheckConstraint('octet_length(token_hash) = 32', name='token_sha256'),)


class Workspace(Identified, Created, Base):
    __tablename__ = 'workspaces'
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    permission_epoch: Mapped[int] = mapped_column(BigInteger, server_default='1')
    migration_key: Mapped[str | None] = mapped_column(String(200), unique=True)


class WorkspaceMember(Base):
    __tablename__ = 'workspace_members'
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey('workspaces.id'), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'), primary_key=True)
    role: Mapped[str] = mapped_column(states('workspace_role', 'admin', 'member'))


class KnowledgeBase(Identified, Created, Base):
    __tablename__ = 'knowledge_bases'
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey('workspaces.id'))
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120))
    active_index_generation_id: Mapped[UUID | None]
    data_epoch: Mapped[int] = mapped_column(BigInteger, server_default='1')
    permission_epoch: Mapped[int] = mapped_column(BigInteger, server_default='1')
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    migration_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    __table_args__ = (
        UniqueConstraint('workspace_id', 'slug'), UniqueConstraint('id', 'workspace_id'),
        ForeignKeyConstraint(['active_index_generation_id', 'id'], ['index_generations.id', 'index_generations.kb_id'],
                             name='fk_kb_active_generation_scope', use_alter=True),
        CheckConstraint('data_epoch > 0 AND permission_epoch > 0', name='positive_epochs'),
    )


class KBMember(Base):
    __tablename__ = 'kb_members'
    kb_id: Mapped[UUID] = mapped_column(ForeignKey('knowledge_bases.id'), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'), primary_key=True)
    role: Mapped[str] = mapped_column(states('kb_role', 'editor', 'viewer'))


class FileObject(Identified, Created, Base):
    __tablename__ = 'file_objects'
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_key: Mapped[str] = mapped_column(Text, unique=True)
    detected_type: Mapped[str] = mapped_column(String(100))
    original_extension: Mapped[str] = mapped_column(String(20))
    gc_pending_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (CheckConstraint('size_bytes >= 0', name='nonnegative_size'),
                      CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name='valid_sha256'))


class LogicalDocument(Identified, Created, Base):
    __tablename__ = 'logical_documents'
    kb_id: Mapped[UUID] = mapped_column(ForeignKey('knowledge_bases.id'))
    display_name: Mapped[str] = mapped_column(String(255))
    normalized_key: Mapped[str] = mapped_column(String(255))
    desired_revision_id: Mapped[UUID | None]
    active_revision_id: Mapped[UUID | None]
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    __table_args__ = (
        UniqueConstraint('id', 'kb_id'),
        Index('uq_live_document_name', 'kb_id', 'normalized_key', unique=True, postgresql_where=sql_text('deleted_at IS NULL')),
        ForeignKeyConstraint(['desired_revision_id', 'id'], ['document_revisions.id', 'document_revisions.document_id'],
                             name='fk_document_desired_scope', use_alter=True),
        ForeignKeyConstraint(['active_revision_id', 'id'], ['document_revisions.id', 'document_revisions.document_id'],
                             name='fk_document_active_scope', use_alter=True),
    )


class DocumentRevision(Identified, Created, Base):
    __tablename__ = 'document_revisions'
    document_id: Mapped[UUID] = mapped_column(ForeignKey('logical_documents.id'))
    revision_no: Mapped[int] = mapped_column(Integer)
    file_object_id: Mapped[UUID | None] = mapped_column(ForeignKey('file_objects.id'))
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(states('revision_status', 'uploaded', 'parsing', 'parsed', 'indexing',
        'ready', 'failed', 'superseded', 'deleted', 'quarantined'))
    parser_profile: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))
    created_by: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    legacy_doc_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    migration_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    source_version_label: Mapped[str | None] = mapped_column(String(60))
    source_file_available: Mapped[bool] = mapped_column(Boolean, server_default=sql_text('true'))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('document_id', 'revision_no'), UniqueConstraint('id', 'document_id'),
                      CheckConstraint('revision_no > 0', name='positive_revision'),
                      CheckConstraint("file_object_id IS NOT NULL OR (status = 'deleted' AND purged_at IS NOT NULL)", name='purged_object'),
                      CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name='valid_sha256'))


class DocumentDeletion(Identified, Base):
    __tablename__ = 'document_deletions'
    document_id: Mapped[UUID] = mapped_column(ForeignKey('logical_documents.id'))
    deleted_by: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    restore_before: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    snapshot: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    __table_args__ = (
        Index('uq_document_current_deletion', 'document_id', unique=True,
              postgresql_where=sql_text('restored_at IS NULL')),
        CheckConstraint('restore_before > deleted_at', name='positive_restore_window'),
        CheckConstraint('restored_at IS NULL OR purged_at IS NULL', name='exclusive_delete_outcome'),
    )


class ParseRun(Identified, Base):
    __tablename__ = 'parse_runs'
    revision_id: Mapped[UUID] = mapped_column(ForeignKey('document_revisions.id'))
    parser_name: Mapped[str] = mapped_column(String(100))
    parser_version: Mapped[str] = mapped_column(String(100))
    config_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(states('parse_status', 'running', 'succeeded', 'failed', 'cancelled'))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    metrics: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))
    __table_args__ = (UniqueConstraint('id', 'revision_id'),)


class ParsedPage(Identified, Base):
    __tablename__ = 'parsed_pages'
    parse_run_id: Mapped[UUID] = mapped_column(ForeignKey('parse_runs.id'))
    page_no: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    details: Mapped[dict[str, JsonValue]] = mapped_column('metadata', JSONB, server_default=sql_text("'{}'::jsonb"))
    __table_args__ = (UniqueConstraint('parse_run_id', 'page_no'), CheckConstraint('page_no > 0', name='positive_page'))


class Chunk(Identified, Base):
    __tablename__ = 'chunks'
    parse_run_id: Mapped[UUID]
    revision_id: Mapped[UUID] = mapped_column(ForeignKey('document_revisions.id'))
    ordinal: Mapped[int] = mapped_column(Integer)
    page_start: Mapped[int] = mapped_column(Integer)
    page_end: Mapped[int] = mapped_column(Integer)
    line_start: Mapped[int] = mapped_column(Integer)
    line_end: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    text_sha256: Mapped[str] = mapped_column(String(64))
    token_count: Mapped[int | None] = mapped_column(Integer)
    legacy_chunk_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    search_tsv: Mapped[str] = mapped_column(TSVECTOR, server_default=sql_text("''::tsvector"))
    __table_args__ = (
        UniqueConstraint('parse_run_id', 'ordinal'), UniqueConstraint('id', 'revision_id'),
        UniqueConstraint('id', 'revision_id', 'parse_run_id', name='uq_chunks_id_revision_parse'),
        ForeignKeyConstraint(['parse_run_id', 'revision_id'], ['parse_runs.id', 'parse_runs.revision_id']),
        CheckConstraint('ordinal >= 0 AND page_start > 0 AND page_end >= page_start AND line_start > 0 AND line_end >= line_start', name='valid_position'),
        Index('ix_chunks_revision_ordinal', 'revision_id', 'ordinal'),
        Index('ix_chunks_search_tsv', 'search_tsv', postgresql_using='gin'),
    )


class ModelProfile(Identified, Created, Base):
    __tablename__ = 'model_profiles'
    provider: Mapped[str] = mapped_column(String(100))
    model_tag: Mapped[str] = mapped_column(String(200))
    model_digest: Mapped[str] = mapped_column(String(100))
    dimension: Mapped[int] = mapped_column(Integer)
    input_profile: Mapped[str] = mapped_column(String(100))
    prompt_profile: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (UniqueConstraint('provider', 'model_digest', 'input_profile'),
                      CheckConstraint('dimension = 1024', name='embedding_dimension'))


class IndexGeneration(Identified, Created, Base):
    __tablename__ = 'index_generations'
    kb_id: Mapped[UUID] = mapped_column(ForeignKey('knowledge_bases.id'))
    model_profile_id: Mapped[UUID] = mapped_column(ForeignKey('model_profiles.id'))
    retrieval_config_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(states('generation_status', 'building', 'ready', 'active', 'superseded', 'failed'))
    corpus_manifest_hash: Mapped[str] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(Integer, server_default='0')
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('id', 'kb_id'),
        Index('uq_generation_active_kb', 'kb_id', unique=True, postgresql_where=sql_text("status = 'active'")),
        CheckConstraint('chunk_count >= 0', name='nonnegative_chunks'))


class GenerationRevision(Base):
    __tablename__ = 'generation_revisions'
    generation_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision_id: Mapped[UUID] = mapped_column(primary_key=True)
    kb_id: Mapped[UUID]
    document_id: Mapped[UUID]
    parse_run_id: Mapped[UUID]
    __table_args__ = (
        ForeignKeyConstraint(['generation_id', 'kb_id'], ['index_generations.id', 'index_generations.kb_id']),
        ForeignKeyConstraint(['revision_id', 'document_id'], ['document_revisions.id', 'document_revisions.document_id']),
        ForeignKeyConstraint(['document_id', 'kb_id'], ['logical_documents.id', 'logical_documents.kb_id']),
        ForeignKeyConstraint(['parse_run_id', 'revision_id'], ['parse_runs.id', 'parse_runs.revision_id']),
        UniqueConstraint('generation_id', 'revision_id', 'parse_run_id'),
    )


class ChunkEmbedding(Created, Base):
    __tablename__ = 'chunk_embeddings'
    generation_id: Mapped[UUID] = mapped_column(primary_key=True)
    chunk_id: Mapped[UUID] = mapped_column(primary_key=True)
    revision_id: Mapped[UUID]
    parse_run_id: Mapped[UUID]
    embedding: Mapped[list[float]] = mapped_column(Vector(1024))
    __table_args__ = (
        ForeignKeyConstraint(['generation_id', 'revision_id', 'parse_run_id'], ['generation_revisions.generation_id', 'generation_revisions.revision_id', 'generation_revisions.parse_run_id']),
        ForeignKeyConstraint(['chunk_id', 'revision_id', 'parse_run_id'], ['chunks.id', 'chunks.revision_id', 'chunks.parse_run_id']),
    )


class Job(Identified, Created, Base):
    __tablename__ = 'jobs'
    type: Mapped[str] = mapped_column(states('job_type', 'parse', 'index', 'query', 'gc', 'eval', 'reindex'))
    workspace_id: Mapped[UUID] = mapped_column(ForeignKey('workspaces.id'))
    kb_id: Mapped[UUID]
    revision_id: Mapped[UUID | None] = mapped_column(ForeignKey('document_revisions.id'))
    generation_id: Mapped[UUID | None]
    state: Mapped[str] = mapped_column(states('job_state', 'queued', 'running', 'retry_wait', 'succeeded', 'failed', 'cancelled', 'superseded'), server_default='queued')
    priority: Mapped[int] = mapped_column(Integer, server_default='0')
    idempotency_key: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, server_default='0')
    max_attempts: Mapped[int] = mapped_column(Integer, server_default='3')
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_by: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    __table_args__ = (
        UniqueConstraint('type', 'idempotency_key'),
        ForeignKeyConstraint(['kb_id', 'workspace_id'], ['knowledge_bases.id', 'knowledge_bases.workspace_id']),
        ForeignKeyConstraint(['generation_id', 'kb_id'], ['index_generations.id', 'index_generations.kb_id']),
        CheckConstraint('attempt_count >= 0 AND max_attempts > 0 AND attempt_count <= max_attempts', name='attempt_budget'),
        CheckConstraint("state != 'running' OR (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)", name='running_lease'),
        Index('ix_jobs_claim', 'state', 'available_at', 'priority', 'created_at'),
        Index('ix_jobs_expired', 'lease_expires_at', postgresql_where=sql_text("state = 'running'")),
    )


class JobAttempt(Identified, Base):
    __tablename__ = 'job_attempts'
    job_id: Mapped[UUID] = mapped_column(ForeignKey('jobs.id'))
    attempt_no: Mapped[int] = mapped_column(Integer)
    worker_id: Mapped[str] = mapped_column(String(200))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(100))
    metrics: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))
    __table_args__ = (UniqueConstraint('job_id', 'attempt_no'),)


class Conversation(Identified, Created, Base):
    __tablename__ = 'conversations'
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    kb_id: Mapped[UUID] = mapped_column(ForeignKey('knowledge_bases.id'))
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QueryRun(Identified, Created, Base):
    __tablename__ = 'query_runs'
    conversation_id: Mapped[UUID | None] = mapped_column(ForeignKey('conversations.id'))
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    kb_id: Mapped[UUID]
    generation_id: Mapped[UUID | None]
    model_profile_id: Mapped[UUID | None] = mapped_column(ForeignKey('model_profiles.id'))
    job_id: Mapped[UUID | None] = mapped_column(ForeignKey('jobs.id'))
    question: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    retrieval_config: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(states('query_status', 'queued', 'running', 'succeeded', 'failed', 'partial', 'cancelled', 'superseded'))
    latencies: Mapped[dict[str, JsonValue]] = mapped_column(JSONB, server_default=sql_text("'{}'::jsonb"))
    auth_version: Mapped[int] = mapped_column(BigInteger)
    permission_epoch: Mapped[int] = mapped_column(BigInteger)
    workspace_permission_epoch: Mapped[int] = mapped_column(BigInteger)
    data_epoch: Mapped[int] = mapped_column(BigInteger)
    response: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    __table_args__ = (ForeignKeyConstraint(['kb_id'], ['knowledge_bases.id']),
        ForeignKeyConstraint(['generation_id', 'kb_id'], ['index_generations.id', 'index_generations.kb_id']),
        Index('ix_query_runs_user_created', 'user_id', 'created_at'))


class QuerySource(Base):
    __tablename__ = 'query_sources'
    query_run_id: Mapped[UUID] = mapped_column(ForeignKey('query_runs.id'), primary_key=True)
    chunk_id: Mapped[UUID] = mapped_column(ForeignKey('chunks.id'), primary_key=True)
    rank: Mapped[int] = mapped_column(Integer)
    channel: Mapped[str] = mapped_column(String(50))
    score: Mapped[float]
    context_origin: Mapped[str] = mapped_column(String(100))


class TraceRecord(Identified, Created, Base):
    __tablename__ = 'trace_records'
    query_run_id: Mapped[UUID] = mapped_column(ForeignKey('query_runs.id'))
    details: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)


class AuditEvent(Base):
    __tablename__ = 'audit_events'
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey('users.id'))
    workspace_id: Mapped[UUID | None] = mapped_column(ForeignKey('workspaces.id'))
    kb_id: Mapped[UUID | None] = mapped_column(ForeignKey('knowledge_bases.id'))
    action: Mapped[str] = mapped_column(String(100))
    resource_type: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[str | None] = mapped_column(String(200))
    outcome: Mapped[str] = mapped_column(String(50))
    request_id: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict[str, JsonValue]] = mapped_column('metadata', JSONB)
    __table_args__ = (Index('ix_audit_workspace_time', 'workspace_id', 'occurred_at'),)


class EvalDataset(Identified, Created, Base):
    __tablename__ = 'eval_datasets'
    kb_id: Mapped[UUID] = mapped_column(ForeignKey('knowledge_bases.id'))
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[str] = mapped_column(String(100))
    frozen_hash: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(states('dataset_status', 'dev', 'holdout'))
    __table_args__ = (UniqueConstraint('kb_id', 'name', 'version'),)


class EvalCase(Identified, Base):
    __tablename__ = 'eval_cases'
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey('eval_datasets.id'))
    question: Mapped[str] = mapped_column(Text)
    scope: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    task_type: Mapped[str] = mapped_column(String(100))
    answerability: Mapped[bool]
    required_facts: Mapped[list[JsonValue]] = mapped_column(JSONB)
    gold_passages: Mapped[list[JsonValue]] = mapped_column(JSONB)
    severity: Mapped[str] = mapped_column(String(50))


class EvalRun(Identified, Created, Base):
    __tablename__ = 'eval_runs'
    dataset_id: Mapped[UUID] = mapped_column(ForeignKey('eval_datasets.id'))
    created_by: Mapped[UUID] = mapped_column(ForeignKey('users.id'))
    code_commit: Mapped[str] = mapped_column(String(64))
    dataset_hash: Mapped[str] = mapped_column(String(64))
    corpus_manifest: Mapped[str] = mapped_column(String(64))
    model_digest: Mapped[str] = mapped_column(String(100))
    prompt_config_hash: Mapped[str] = mapped_column(String(64))
    retrieval_config_hash: Mapped[str] = mapped_column(String(64))


class EvalResult(Base):
    __tablename__ = 'eval_results'
    run_id: Mapped[UUID] = mapped_column(ForeignKey('eval_runs.id'), primary_key=True)
    case_id: Mapped[UUID] = mapped_column(ForeignKey('eval_cases.id'), primary_key=True)
    retrieved_ids: Mapped[list[JsonValue]] = mapped_column(JSONB)
    generation_ids: Mapped[list[JsonValue]] = mapped_column(JSONB)
    automatic_metrics: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)
    human_scores: Mapped[dict[str, JsonValue] | None] = mapped_column(JSONB)
    reviewer_ids: Mapped[list[JsonValue]] = mapped_column(JSONB)
    dispute_status: Mapped[str] = mapped_column(String(50))


class LegacyAlias(Base):
    __tablename__ = 'legacy_aliases'
    # Survives physical GC. Target IDs deliberately are historical metadata, not FKs.
    kind: Mapped[str] = mapped_column(states('alias_kind', 'document', 'chunk'), primary_key=True)
    legacy_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    kb_id: Mapped[UUID] = mapped_column(ForeignKey('knowledge_bases.id'))
    target_id: Mapped[UUID]
    revision_id: Mapped[UUID]
    text_sha256: Mapped[str | None] = mapped_column(String(64))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MigrationCheckpoint(Base):
    __tablename__ = 'migration_checkpoints'
    source_db_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    legacy_doc_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision_id: Mapped[UUID] = mapped_column(ForeignKey('document_revisions.id'))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BootstrapState(Base):
    __tablename__ = 'bootstrap_state'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    consumed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (CheckConstraint('id = 1', name='singleton'),)


class AuthRateLimit(Base):
    __tablename__ = 'auth_rate_limits'
    bucket_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    hits: Mapped[int] = mapped_column(Integer)
    __table_args__ = (Index('ix_auth_rate_limits_window', 'window_started_at'),)


class ApiReceipt(Created, Base):
    __tablename__ = 'api_receipts'
    user_id: Mapped[UUID] = mapped_column(ForeignKey('users.id'), primary_key=True)
    operation: Mapped[str] = mapped_column(String(200), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict[str, JsonValue]] = mapped_column(JSONB)


# PostgreSQL does not index referencing columns automatically. Ensure every FK
# has a matching leading index (including reverse lookups on composite keys).
for _table in Base.metadata.tables.values():
    for _constraint in _table.foreign_key_constraints:
        _columns = list(_constraint.columns)
        _names = [column.name for column in _columns]
        _indexes = [list(index.columns) for index in _table.indexes]
        _indexes += [list(_table.primary_key.columns)]
        _indexes += [list(item.columns) for item in _table.constraints if isinstance(item, UniqueConstraint)]
        if not any([column.name for column in index[:len(_names)]] == _names for index in _indexes):
            Index('ix_' + _table.name + '_' + '_'.join(_names), *_columns)
