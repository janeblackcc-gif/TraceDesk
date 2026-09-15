from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import replace
from collections.abc import Callable

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert

from app.db.models import Chunk, ChunkEmbedding, GenerationRevision, IndexGeneration, Job, KnowledgeBase, LogicalDocument, ModelProfile
from app.db.session import Database
from app.models.profile import EmbeddingIdentity, EmbeddingProvider
from app.services.errors import DomainError
from app.services.index_service import IndexService, corpus, manifest_hash
from .heartbeat import lease_heartbeat
from .repository import JobRepository, Lease


class IndexHandler:
    def __init__(self, database: Database, provider: Callable[[threading.Event], EmbeddingProvider]):
        self.database, self.provider = database, provider

    def run(self, queue: JobRepository, lease: Lease) -> str:
        try:
            with lease_heartbeat(queue, lease) as cancel:
                provider = self.provider(cancel)
                if lease.generation_id is None:
                    identity = provider.identity()
                    with self.database.transaction() as session:
                        session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                                        {'key': 'kb-mutation:' + str(lease.kb_id)})
                        job = queue._owned(session, lease)
                        if job is None:
                            return 'lease_lost'
                        if job.cancel_requested_at is not None:
                            queue._terminal(session, job, 'cancelled')
                            return 'cancelled'
                        kb = session.get(KnowledgeBase, lease.kb_id)
                        if kb is None or kb.deleted_at is not None:
                            queue._terminal(session, job, 'superseded')
                            return 'superseded'
                        result = IndexService.schedule(session, kb, job.created_by, identity, running_job=job)
                        generation_id = job.generation_id
                    if generation_id is None:
                        return queue.finish(lease, lambda session, job: result)
                    lease = replace(lease, generation_id=generation_id)
                with self.database.transaction() as session:
                    generation = session.get(IndexGeneration, lease.generation_id)
                    profile = session.get(ModelProfile, generation.model_profile_id) if generation else None
                    if generation is None or profile is None:
                        raise DomainError('INDEX_GENERATION_UNAVAILABLE', 409)
                    identity = EmbeddingIdentity(profile.provider, profile.model_tag, profile.model_digest,
                                                 profile.dimension, profile.input_profile)
                    rows = session.execute(select(Chunk, LogicalDocument.display_name).join(GenerationRevision,
                        (GenerationRevision.revision_id == Chunk.revision_id) & (GenerationRevision.parse_run_id == Chunk.parse_run_id))
                        .join(LogicalDocument, LogicalDocument.id == GenerationRevision.document_id)
                        .where(GenerationRevision.generation_id == generation.id).order_by(Chunk.id)).all()
                    inputs = [(chunk.id, chunk.revision_id, chunk.parse_run_id, chunk.text_sha256,
                               chunk.text, f'{filename}\n{chunk.heading}\n{chunk.text}') for chunk, filename in rows]
                for start in range(0, len(inputs), 8):
                    if cancel.is_set():
                        return queue.finish(lease, lambda session, job: {})
                    if provider.identity() != identity:
                        raise DomainError('MODEL_IDENTITY_CHANGED', 409)
                    batch = inputs[start:start + 8]
                    if any(hashlib.sha256(item[4].encode()).hexdigest() != item[3] for item in batch):
                        raise DomainError('INDEX_CONTENT_HASH_MISMATCH', 409)
                    values = provider.embed([item[5] for item in batch])
                    if len(values) != len(batch) or any(len(v) != 1024 or any(isinstance(x, bool) or not math.isfinite(x) for x in v)
                            or not 0 < sum(x * x for x in v) < math.inf for v in values):
                        raise DomainError('MODEL_VECTOR_INVALID', 503)
                    if provider.identity() != identity:
                        raise DomainError('MODEL_IDENTITY_CHANGED', 409)
                    with self.database.transaction() as session:
                        session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                                        {'key': 'kb-mutation:' + str(lease.kb_id)})
                        job = queue._owned(session, lease)
                        if job is None:
                            return 'lease_lost'
                        if job.cancel_requested_at is not None:
                            queue._terminal(session, job, 'cancelled')
                            return 'cancelled'
                        generation = session.get(IndexGeneration, lease.generation_id)
                        try:
                            current = manifest_hash(corpus(session, lease.kb_id))
                        except DomainError:
                            current = None
                        if generation is None or generation.status != 'building' or current != generation.corpus_manifest_hash:
                            queue._terminal(session, job, 'superseded', 'INPUT_CHANGED')
                            if generation and generation.status == 'building':
                                generation.status = 'superseded'
                            return 'superseded'
                        for item, vector in zip(batch, values):
                            session.execute(insert(ChunkEmbedding).values(generation_id=generation.id, chunk_id=item[0],
                                revision_id=item[1], parse_run_id=item[2], embedding=vector)
                                .on_conflict_do_nothing(index_elements=['generation_id', 'chunk_id']))
                if provider.identity() != identity:
                    raise DomainError('MODEL_IDENTITY_CHANGED', 409)
                return queue.finish(lease, IndexService.activate)
        except DomainError as exc:
            return queue.fail(lease, exc.code, retryable=exc.retryable, effect=self.record_failure)

    @staticmethod
    def record_failure(session, job: Job) -> None:
        generation = session.get(IndexGeneration, job.generation_id) if job.generation_id else None
        if generation and generation.status == 'building' and job.state in {'failed', 'cancelled'}:
            generation.status = 'failed'
