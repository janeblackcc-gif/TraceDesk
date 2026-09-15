from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.schemas import Claim, QueryRequest, QueryResponse, Source
from app.authz.policy import recheck
from app.db.models import (Conversation, IndexGeneration, Job, JsonValue, ModelProfile, QueryRun, QuerySource, TraceRecord)
from app.db.session import Database
from app.models.profile import EmbeddingProvider
from app.rag.sparse import retrieve
from app.retrieval import retrieval_queries
from app.service import Service
from app.services.errors import DomainError
from app.services.query_service import QueryService, pending_response, snapshot_for
from .heartbeat import lease_heartbeat
from .repository import JobRepository, Lease


class QueryProvider(EmbeddingProvider, Protocol):
    def generate(self, question: str, evidence: list[dict]) -> dict: ...


class QueryHandler:
    def __init__(self, database: Database, provider: Callable[[threading.Event], QueryProvider]):
        self.database, self.provider = database, provider

    def run(self, queue: JobRepository, lease: Lease) -> str:
        started = time.perf_counter()
        try:
            with lease_heartbeat(queue, lease) as cancel:
                with self.database.transaction() as session:
                    job = queue._owned(session, lease)
                    if job is None:
                        return 'lease_lost'
                    if job.cancel_requested_at is not None:
                        queue._terminal(session, job, 'cancelled')
                        return 'cancelled'
                    run = session.scalar(select(QueryRun).where(QueryRun.job_id == job.id))
                    if run is None:
                        raise DomainError('QUERY_RECORD_INVALID', 409)
                    snapshot = snapshot_for(run)
                    recheck(session, snapshot)
                    body = QueryRequest.model_validate({'knowledge_base_id': run.kb_id, 'question': run.question,
                        'conversation_id': run.conversation_id, **run.retrieval_config})
                    run.status = 'running'
                    response = pending_response(run)
                    response.timing.queue_ms = max(0, (datetime.now(timezone.utc) - run.created_at).total_seconds() * 1000)
                    query_id, generation_id = run.id, run.generation_id
                    previous = session.scalar(select(QueryRun.question).where(QueryRun.conversation_id == run.conversation_id,
                        QueryRun.id != run.id, QueryRun.data_epoch == run.data_epoch, QueryRun.status == 'succeeded')
                        .order_by(QueryRun.created_at.desc()).limit(1))
                    generation = session.get(IndexGeneration, generation_id) if generation_id else None
                    profile = session.get(ModelProfile, generation.model_profile_id) if generation else None
                    expected_digest = profile.model_digest if profile else None
                    expected_input = profile.input_profile if profile else None
                    expected_provider = profile.provider if profile else None
                followup = bool(re.match(r'^(那|它|这个|这一步|上述|刚才)', body.question))
                if followup and not previous:
                    response.status, response.warning = 'clarify', '缺少同一知识库版本的上文，请写出具体组件或问题。'
                    return self.finish(queue, lease, query_id, response, started)
                if followup:
                    response.effective_question = f'{previous}\n追问：{body.question}'
                queries = retrieval_queries(response.effective_question) if body.profile == 'ollama' else [response.effective_question]
                provider = self.provider(cancel) if body.profile == 'ollama' else None
                vectors = None
                technical_error = None
                if provider is not None and body.method != 'bm25':
                    if generation_id is None or expected_digest is None:
                        raise DomainError('INDEX_REQUIRED', 409)
                    try:
                        identity = provider.identity()
                        if (identity.digest != expected_digest or identity.provider != expected_provider or
                                identity.input_profile != expected_input):
                            raise DomainError('INDEX_STALE', 409)
                        vectors = provider.embed(['Instruct: Retrieve technical documentation passages that answer the question.\nQuery: ' + query for query in queries])
                        if provider.identity() != identity:
                            raise DomainError('INDEX_STALE', 409)
                    except DomainError as exc:
                        if exc.code in {'INDEX_STALE', 'MODEL_CANCELLED'}:
                            raise
                        technical_error = exc.code
                retrieval_started = time.perf_counter()
                with self.database.transaction() as session:
                    recheck(session, snapshot)
                    sources = retrieve(session, body.knowledge_base_id, generation_id, queries,
                        'hybrid' if technical_error else body.method, vectors, context=body.profile == 'ollama')
                response.timing.retrieval_ms = (time.perf_counter() - retrieval_started) * 1000
                response.sources = [Source.model_validate(source) for source in sources]
                mentioned = set(re.findall(r'(?<![A-Za-z0-9_])v\d+(?:\.\d+)*(?![A-Za-z0-9_])', body.question, re.I))
                versions = {source.version.lower() for source in response.sources if re.fullmatch(r'v\d+(?:\.\d+)*', source.version, re.I)}
                if mentioned and versions and {value.lower() for value in mentioned} != versions:
                    response.status, response.warning, response.sources = 'needs_scope', '问题涉及其他资料版本，请先选择对应知识库。', []
                elif not sources:
                    response.status, response.warning = 'no_evidence', '当前知识库未检索到足够相关的证据。'
                elif technical_error:
                    self.partial(response, technical_error)
                elif provider is None:
                    response.status = 'evidence_found'
                    response.claims = [Claim.model_validate({'text': row['text'][:450],
                        'citations': [{'chunk_id': row['id'], 'quote': row['text'][:450]}]}) for row in sources[:4]]
                else:
                    with self.database.transaction() as session:
                        recheck(session, snapshot)
                    if cancel.is_set():
                        raise DomainError('MODEL_CANCELLED', 409)
                    generation_started = time.perf_counter()
                    try:
                        generated = provider.generate(response.effective_question, sources)
                        claims = Service.validate_claims(generated, sources)
                        if claims is None:
                            self.partial(response, 'MODEL_CITATION_INVALID')
                        elif not claims:
                            response.status, response.warning = 'no_evidence', '现有证据缺少必要事实，未生成答案。'
                        else:
                            response.status = 'answered'
                            response.claims = [Claim.model_validate(claim) for claim in claims]
                        assessment = generated.get('generation_assessment')
                        if isinstance(assessment, dict):
                            # QueryResponse validates the provider's JSON boundary.
                            checked = QueryResponse.model_validate({**response.model_dump(), 'generation_assessment': assessment})
                            response.generation_assessment = checked.generation_assessment
                    except DomainError as exc:
                        if exc.code == 'MODEL_CANCELLED':
                            raise
                        self.partial(response, exc.code)
                    finally:
                        response.timing.generation_ms = (time.perf_counter() - generation_started) * 1000
                return self.finish(queue, lease, query_id, response, started)
        except DomainError as exc:
            authorization_changed = exc.code == 'AUTHORIZATION_CHANGED'
            return queue.fail(lease, exc.code, effect=lambda session, job: QueryService.redact(session, job,
                auth_changed=authorization_changed))

    @staticmethod
    def partial(response: QueryResponse, code: str) -> None:
        response.status, response.error_code = 'partial', code
        response.claims = []
        response.warning = '模型步骤未完成，当前仅提供已检索的原文证据。'

    @staticmethod
    def finish(queue: JobRepository, lease: Lease, query_id: UUID, response: QueryResponse, started: float) -> str:
        response.timing.total_ms = response.timing.queue_ms + (time.perf_counter() - started) * 1000
        def persist(session: Session, job: Job) -> dict[str, JsonValue]:
            run = session.get(QueryRun, query_id, with_for_update=True)
            if run is None:
                raise DomainError('QUERY_RECORD_INVALID', 409)
            recheck(session, snapshot_for(run))
            run.status = 'partial' if response.status == 'partial' else 'succeeded'
            run.response = response.model_dump(mode='json')
            run.latencies = response.timing.model_dump()
            for source in response.sources:
                session.add(QuerySource(query_run_id=run.id, chunk_id=source.id, rank=source.rank,
                    channel='retrieval', score=source.score, context_origin=source.context_origin))
            session.add(TraceRecord(query_run_id=run.id, details={'timing': run.latencies,
                'generation_id': str(run.generation_id) if run.generation_id else None,
                'retrieval_config': run.retrieval_config, 'source_ids': [str(source.id) for source in response.sources],
                'status': response.status, 'error_code': response.error_code}))
            if run.conversation_id:
                session.get(Conversation, run.conversation_id).last_activity_at = session.scalar(select(func.clock_timestamp()))
            return {'query_id': str(run.id), 'status': response.status}
        return queue.finish(lease, persist)
