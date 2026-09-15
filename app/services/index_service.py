from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.audit.service import audit
from app.authz.policy import kb_access
from app.db.models import (ApiReceipt, Chunk, ChunkEmbedding, DocumentRevision, GenerationRevision,
                           IndexGeneration, Job, JsonValue, KnowledgeBase, LogicalDocument, ModelProfile, ParseRun)
from app.db.session import Database
from app.jobs.repository import JobRepository, Superseded, TERMINAL, enqueue, payload_hash
from app.models.profile import EmbeddingIdentity, EmbeddingProvider
from .errors import DomainError

RETRIEVAL_CONFIG = {'sparse': 'rc2-bm25-lexical-v1', 'dense': 'pgvector-exact-v1', 'rrf_k': 60}
RETRIEVAL_HASH = payload_hash(RETRIEVAL_CONFIG)


@dataclass(frozen=True)
class CorpusRevision:
    document_id: UUID
    revision_id: UUID
    parse_run_id: UUID
    filename: str
    sha256: str
    chunks: tuple[tuple[UUID, str], ...]

    def manifest(self) -> dict[str, JsonValue]:
        return {'document_id': str(self.document_id), 'revision_id': str(self.revision_id),
                'parse_run_id': str(self.parse_run_id), 'filename': self.filename, 'sha256': self.sha256,
                'chunks': [[str(identifier), digest] for identifier, digest in self.chunks]}


def corpus(session: Session, kb_id: UUID) -> list[CorpusRevision]:
    result = []
    documents = session.scalars(select(LogicalDocument).where(LogicalDocument.kb_id == kb_id,
        LogicalDocument.deleted_at.is_(None)).order_by(LogicalDocument.id)).all()
    for document in documents:
        revision = session.get(DocumentRevision, document.desired_revision_id) if document.desired_revision_id else None
        if revision is not None and revision.status == 'quarantined':
            continue
        if revision is None or revision.deleted_at is not None or revision.status not in {'parsed', 'indexing', 'ready', 'superseded'}:
            raise DomainError('INDEX_PARSE_PENDING', 409)
        run = session.scalar(select(ParseRun).where(ParseRun.revision_id == revision.id, ParseRun.status == 'succeeded')
            .order_by(ParseRun.finished_at.desc().nulls_last(), ParseRun.id).limit(1))
        if run is None:
            raise DomainError('INDEX_PARSE_PENDING', 409)
        chunks = tuple((row.id, row.text_sha256) for row in session.scalars(select(Chunk).where(Chunk.parse_run_id == run.id)
            .order_by(Chunk.ordinal)))
        result.append(CorpusRevision(document.id, revision.id, run.id, document.display_name, revision.sha256, chunks))
    if not result or sum(len(item.chunks) for item in result) == 0:
        raise DomainError('INDEX_EMPTY_CORPUS', 409)
    if sum(len(item.chunks) for item in result) > 50000:
        raise DomainError('CORPUS_CAPACITY_EXCEEDED', 429)
    return result


def manifest_hash(items: list[CorpusRevision]) -> str:
    return payload_hash({'revisions': [item.manifest() for item in items]})


class IndexService:
    def __init__(self, database: Database):
        self.database = database

    def request(self, actor: UUID, kb_id: UUID, key: str, request_id: str, provider: EmbeddingProvider,
                *, profile_id: UUID | None = None, force: bool = False) -> dict[str, JsonValue]:
        if not 1 <= len(key) <= 200:
            raise DomainError('IDEMPOTENCY_KEY_REQUIRED', 422)
        request_hash = payload_hash({'profile_id': str(profile_id) if profile_id else None, 'force': force})
        with self.database.transaction() as session:
            kb_access(session, actor, kb_id, 'editor')
            receipt = session.get(ApiReceipt, (actor, 'index:' + str(kb_id), key))
            if receipt:
                if receipt.payload_hash != request_hash:
                    raise DomainError('IDEMPOTENCY_CONFLICT', 409)
                return dict(receipt.result)
        identity = provider.identity()
        with self.database.transaction() as session:
            session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                            {'key': 'kb-mutation:' + str(kb_id)})
            kb = kb_access(session, actor, kb_id, 'editor')
            receipt = session.get(ApiReceipt, (actor, 'index:' + str(kb_id), key))
            if receipt:
                if receipt.payload_hash != request_hash:
                    raise DomainError('IDEMPOTENCY_CONFLICT', 409)
                return dict(receipt.result)
            if profile_id is not None:
                selected = session.get(ModelProfile, profile_id)
                if (selected is None or selected.provider != identity.provider or selected.model_digest != identity.digest or
                        selected.input_profile != identity.input_profile):
                    raise DomainError('MODEL_PROFILE_CHANGED', 409)
            result = self.schedule(session, kb, actor, identity, force=force)
            session.add(ApiReceipt(user_id=actor, operation='index:' + str(kb_id), key=key,
                payload_hash=request_hash, result=result))
            audit(session, 'index.request', actor=actor, request_id=request_id, workspace_id=kb.workspace_id,
                  kb_id=kb.id, resource_type='generation', resource_id=result['generation_id'])
            return result

    @staticmethod
    def schedule(session: Session, kb: KnowledgeBase, actor: UUID, identity: EmbeddingIdentity,
                 running_job: Job | None = None, *, force: bool = False) -> dict[str, JsonValue]:
        if identity.dimension != 1024 or not identity.digest or len(identity.digest) != 64:
            raise DomainError('MODEL_IDENTITY_UNAVAILABLE', 503)
        items = corpus(session, kb.id)
        session.execute(insert(ModelProfile).values(provider=identity.provider, model_tag=identity.tag,
            model_digest=identity.digest, dimension=identity.dimension, input_profile=identity.input_profile)
            .on_conflict_do_nothing(index_elements=['provider', 'model_digest', 'input_profile']))
        profile = session.scalar(select(ModelProfile).where(ModelProfile.provider == identity.provider,
            ModelProfile.model_digest == identity.digest, ModelProfile.input_profile == identity.input_profile))
        if profile is None:
            raise DomainError('MODEL_PROFILE_UNAVAILABLE', 503)
        manifest = manifest_hash(items)
        active = session.get(IndexGeneration, kb.active_index_generation_id) if kb.active_index_generation_id else None
        if (not force and active and active.corpus_manifest_hash == manifest and active.model_profile_id == profile.id and
                active.retrieval_config_hash == RETRIEVAL_HASH):
            return {'generation_id': str(active.id), 'job_id': None, 'state': 'active'}
        pending = session.scalars(select(Job).where(Job.kb_id == kb.id, Job.type.in_(['index', 'reindex']),
            ~Job.state.in_(TERMINAL)).order_by(Job.id).with_for_update()).all()
        for job in pending:
            if running_job is not None and job.id == running_job.id:
                continue
            generation = session.get(IndexGeneration, job.generation_id) if job.generation_id else None
            if (not force and generation and generation.corpus_manifest_hash == manifest and generation.model_profile_id == profile.id and
                    job.cancel_requested_at is None):
                return {'generation_id': str(generation.id), 'job_id': str(job.id), 'state': job.state}
            JobRepository._terminal(session, job, 'superseded', 'INPUT_CHANGED')
            if generation and generation.status == 'building':
                generation.status = 'superseded'
        generation = IndexGeneration(kb_id=kb.id, model_profile_id=profile.id, retrieval_config_hash=RETRIEVAL_HASH,
            corpus_manifest_hash=manifest, status='building', chunk_count=sum(len(item.chunks) for item in items))
        session.add(generation)
        session.flush()
        for item in items:
            session.add(GenerationRevision(generation_id=generation.id, kb_id=kb.id, document_id=item.document_id,
                revision_id=item.revision_id, parse_run_id=item.parse_run_id))
        if running_job is None:
            job = enqueue(session, kind='index', workspace_id=kb.workspace_id, kb_id=kb.id, created_by=actor,
                key='index:' + str(generation.id), generation_id=generation.id,
                payload={'generation_id': str(generation.id), 'manifest': manifest})
        else:
            job = running_job
            job.generation_id = generation.id
        return {'generation_id': str(generation.id), 'job_id': str(job.id), 'state': 'queued'}

    @staticmethod
    def after_parse(session: Session, completed: Job) -> None:
        kb = session.get(KnowledgeBase, completed.kb_id)
        # Do not publish a partial desired corpus. The last successful parser
        # schedules the complete snapshot; failure remains visible on its revision.
        try:
            corpus(session, kb.id)
        except DomainError as exc:
            if exc.code in {'INDEX_PARSE_PENDING', 'INDEX_EMPTY_CORPUS'}:
                return
            raise
        for pending in session.scalars(select(Job).where(Job.kb_id == kb.id,
                Job.type.in_(['index', 'reindex']), ~Job.state.in_(TERMINAL)).with_for_update()):
            if pending.type == 'reindex' and pending.generation_id is None and pending.state == 'queued':
                return
            JobRepository._terminal(session, pending, 'superseded', 'INPUT_CHANGED')
            generation = session.get(IndexGeneration, pending.generation_id) if pending.generation_id else None
            if generation and generation.status == 'building':
                generation.status = 'superseded'
        enqueue(session, kind='reindex', workspace_id=kb.workspace_id, kb_id=kb.id, created_by=completed.created_by,
                key='auto-index:' + uuid4().hex, payload={'reason': 'parse_completed'}, completing_job=completed)

    @staticmethod
    def activate(session: Session, job: Job) -> dict[str, JsonValue]:
        kb = session.get(KnowledgeBase, job.kb_id, with_for_update=True)
        generation = session.get(IndexGeneration, job.generation_id, with_for_update=True)
        if kb is None or generation is None or generation.status != 'building':
            raise Superseded()
        try:
            items = corpus(session, kb.id)
        except DomainError:
            raise Superseded() from None
        if manifest_hash(items) != generation.corpus_manifest_hash:
            raise Superseded()
        count = session.scalar(select(func.count()).select_from(ChunkEmbedding).where(ChunkEmbedding.generation_id == generation.id))
        if count != generation.chunk_count:
            raise DomainError('INDEX_INCOMPLETE', 409)
        old = session.get(IndexGeneration, kb.active_index_generation_id) if kb.active_index_generation_id else None
        if old:
            old.status = 'superseded'
            session.flush()
        now = session.scalar(select(func.clock_timestamp()))
        generation.status, generation.activated_at = 'active', now
        kb.active_index_generation_id = generation.id
        kb.data_epoch += 1
        for item in items:
            document = session.get(LogicalDocument, item.document_id)
            if document.active_revision_id and document.active_revision_id != item.revision_id:
                session.get(DocumentRevision, document.active_revision_id).status = 'superseded'
            document.active_revision_id = item.revision_id
            revision = session.get(DocumentRevision, item.revision_id)
            revision.status, revision.activated_at = 'ready', now
        return {'generation_id': str(generation.id), 'state': 'active', 'chunk_count': count}

    def rollback(self, actor: UUID, kb_id: UUID, generation_id: UUID, request_id: str,
                 *, require_current_manifest: bool = False) -> None:
        with self.database.transaction() as session:
            session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                            {'key': 'kb-mutation:' + str(kb_id)})
            kb = kb_access(session, actor, kb_id, 'admin')
            target = session.get(IndexGeneration, generation_id)
            if (target is None or target.kb_id != kb.id or target.activated_at is None or
                    target.status not in {'active', 'superseded'}):
                raise DomainError('NOT_FOUND', 404)
            if require_current_manifest and manifest_hash(corpus(session, kb.id)) != target.corpus_manifest_hash:
                raise DomainError('INDEX_MANIFEST_CHANGED', 409)
            members = session.scalars(select(GenerationRevision).where(GenerationRevision.generation_id == target.id)).all()
            live_documents = set(session.scalars(select(LogicalDocument.id).where(LogicalDocument.kb_id == kb.id, LogicalDocument.deleted_at.is_(None))))
            if live_documents != {member.document_id for member in members}:
                raise DomainError('ROLLBACK_CORPUS_CHANGED', 409)
            for member in members:
                revision = session.get(DocumentRevision, member.revision_id)
                if revision is None or revision.deleted_at is not None or revision.purged_at is not None:
                    raise DomainError('ROLLBACK_SOURCE_GONE', 409)
            count = session.scalar(select(func.count()).select_from(ChunkEmbedding).where(ChunkEmbedding.generation_id == target.id))
            if count != target.chunk_count:
                raise DomainError('ROLLBACK_INDEX_GONE', 409)
            old = session.get(IndexGeneration, kb.active_index_generation_id) if kb.active_index_generation_id else None
            if old and old.id != target.id:
                old.status = 'superseded'
                session.flush()
            target.status = 'active'
            kb.active_index_generation_id = target.id
            kb.data_epoch += 1
            for member in members:
                document = session.get(LogicalDocument, member.document_id)
                document.active_revision_id = document.desired_revision_id = member.revision_id
                session.get(DocumentRevision, member.revision_id).status = 'ready'
            for job in session.scalars(select(Job).where(Job.kb_id == kb.id, Job.type.in_(['index', 'reindex']), ~Job.state.in_(TERMINAL)).with_for_update()):
                JobRepository._terminal(session, job, 'superseded', 'INDEX_ROLLED_BACK')
            audit(session, 'index.rollback', actor=actor, request_id=request_id, workspace_id=kb.workspace_id,
                  kb_id=kb.id, resource_type='generation', resource_id=str(target.id))
