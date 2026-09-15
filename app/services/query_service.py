from __future__ import annotations

from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.api.schemas import QueryRequest, QueryResponse
from app.audit.service import audit
from app.authz.policy import AuthorizationSnapshot, capture, kb_access, recheck
from app.db.models import (ApiReceipt, Conversation, Job, JsonValue, KnowledgeBase, QueryRun,
                           QuerySource, TraceRecord)
from app.db.session import Database
from app.jobs.repository import enqueue, payload_hash
from .errors import DomainError


def snapshot_for(run: QueryRun) -> AuthorizationSnapshot:
    return AuthorizationSnapshot(run.user_id, run.kb_id, run.auth_version, run.permission_epoch,
                                 run.workspace_permission_epoch, run.data_epoch)


def pending_response(run: QueryRun, status: str | None = None, error: str | None = None) -> QueryResponse:
    # Validation is kept at this database-to-API boundary, including stored enums.
    return QueryResponse.model_validate({'query_id': run.id, 'query_job_id': run.job_id,
        'conversation_id': run.conversation_id, 'status': status or run.status, 'question': run.question,
        'effective_question': run.question, 'scope': {'kb_id': run.kb_id, 'data_epoch': run.data_epoch,
        'index_generation_id': run.generation_id}, 'error_code': error, 'timing': run.latencies})


class QueryService:
    def __init__(self, database: Database):
        self.database = database

    def create(self, actor: UUID, body: QueryRequest, key: str, request_id: str) -> QueryResponse:
        if not 1 <= len(key) <= 200:
            raise DomainError('IDEMPOTENCY_KEY_REQUIRED', 422)
        payload = body.model_dump(mode='json')
        digest = payload_hash(payload)
        with self.database.transaction() as session:
            session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                            {'key': 'query-admission:' + str(actor)})
            snapshot = capture(session, actor, body.knowledge_base_id)
            receipt = session.get(ApiReceipt, (actor, 'query', key))
            if receipt:
                if receipt.payload_hash != digest:
                    raise DomainError('IDEMPOTENCY_CONFLICT', 409)
                identifier = receipt.result.get('query_id')
                if not isinstance(identifier, str):
                    raise DomainError('QUERY_RECORD_INVALID', 503)
                run = session.get(QueryRun, UUID(identifier))
                if run is None:
                    raise DomainError('NOT_FOUND', 404)
                return self.read_in_session(session, actor, run)
            kb = session.get(KnowledgeBase, body.knowledge_base_id)
            if body.conversation_id is not None:
                conversation = session.get(Conversation, body.conversation_id)
                if (conversation is None or conversation.user_id != actor or conversation.kb_id != kb.id or
                        conversation.deleted_at is not None):
                    raise DomainError('NOT_FOUND', 404)
            else:
                conversation = Conversation(user_id=actor, kb_id=kb.id)
                session.add(conversation)
                session.flush()
            run = QueryRun(user_id=actor, kb_id=kb.id, conversation_id=conversation.id, question=body.question,
                generation_id=kb.active_index_generation_id, scope={'kb_id': str(kb.id)},
                retrieval_config={'profile': body.profile, 'method': body.method}, status='queued',
                auth_version=snapshot.auth_version, permission_epoch=snapshot.kb_permission_epoch,
                workspace_permission_epoch=snapshot.workspace_permission_epoch, data_epoch=snapshot.data_epoch)
            session.add(run)
            session.flush()
            job = enqueue(session, kind='query', workspace_id=kb.workspace_id, kb_id=kb.id, created_by=actor,
                key='query:' + str(run.id), generation_id=run.generation_id, payload={'query_id': str(run.id)})
            run.job_id = job.id
            result = {'query_id': str(run.id)}
            session.add(ApiReceipt(user_id=actor, operation='query', key=key, payload_hash=digest, result=result))
            audit(session, 'query.create', actor=actor, request_id=request_id, workspace_id=kb.workspace_id,
                  kb_id=kb.id, resource_type='query', resource_id=str(run.id))
            return pending_response(run)

    @staticmethod
    def authorize(session: Session, actor: UUID, run: QueryRun) -> None:
        kb_access(session, actor, run.kb_id, 'viewer' if actor == run.user_id else 'admin')
        recheck(session, snapshot_for(run))

    @staticmethod
    def read_in_session(session: Session, actor: UUID, run: QueryRun) -> QueryResponse:
        QueryService.authorize(session, actor, run)
        if run.scope.get('retention') == 'expired':
            raise DomainError('QUERY_RESULT_EXPIRED', 410)
        job = session.get(Job, run.job_id) if run.job_id else None
        if job and job.state in {'cancelled', 'superseded', 'failed'}:
            return pending_response(run, job.state, job.error_code)
        if run.response is not None:
            return QueryResponse.model_validate(run.response)
        return pending_response(run, 'running' if job and job.state == 'running' else run.status,
                                job.error_code if job else None)

    def read(self, actor: UUID, query_id: UUID) -> QueryResponse:
        with self.database.transaction() as session:
            run = session.get(QueryRun, query_id)
            if run is None:
                raise DomainError('NOT_FOUND', 404)
            return self.read_in_session(session, actor, run)

    def trace(self, actor: UUID, query_id: UUID) -> dict[str, JsonValue]:
        with self.database.transaction() as session:
            run = session.get(QueryRun, query_id)
            if run is None:
                raise DomainError('NOT_FOUND', 404)
            self.authorize(session, actor, run)
            if run.scope.get('retention') == 'expired':
                raise DomainError('QUERY_RESULT_EXPIRED', 410)
            trace = session.scalar(select(TraceRecord).where(TraceRecord.query_run_id == run.id))
            return {'query_id': str(run.id), 'status': run.status, 'details': dict(trace.details) if trace else {}}

    def export(self, actor: UUID, query_id: UUID, request_id: str) -> str:
        with self.database.transaction() as session:
            run = session.get(QueryRun, query_id)
            if run is None:
                raise DomainError('NOT_FOUND', 404)
            result = self.read_in_session(session, actor, run)
            if result.status in {'queued', 'running', 'failed', 'cancelled', 'superseded'}:
                raise DomainError('QUERY_NOT_READY', 409)
            # Escape raw HTML; the export is text and never executes document code.
            def safe(value: str) -> str:
                return value.replace('<', '&lt;').replace('>', '&gt;')
            lines = ['# TraceDesk 查询记录', '', safe(result.question), '', '状态：' + result.status, '']
            for claim in result.claims:
                lines.append(safe(claim.text))
                for citation in claim.citations:
                    lines += ['', f'来源：{citation.chunk_id}', *('> ' + safe(line) for line in citation.quote.splitlines()), '']
            if not result.claims:
                for source in result.sources:
                    lines += [f'## {safe(source.filename)} · {source.page}', '', *('> ' + safe(line) for line in source.text.splitlines()), '']
            if result.warning:
                lines += [safe(result.warning), '']
            kb = session.get(KnowledgeBase, run.kb_id)
            audit(session, 'query.export', actor=actor, request_id=request_id, workspace_id=kb.workspace_id,
                  kb_id=kb.id, resource_type='query', resource_id=str(run.id))
            return '\n'.join(lines)

    @staticmethod
    def redact(session: Session, job: Job, *, auth_changed: bool = False) -> None:
        run = session.scalar(select(QueryRun).where(QueryRun.job_id == job.id))
        if run:
            run.status = 'superseded' if auth_changed else job.state
            run.response = None
            if auth_changed:
                run.question = ''
            session.execute(delete(QuerySource).where(QuerySource.query_run_id == run.id))
            session.execute(delete(TraceRecord).where(TraceRecord.query_run_id == run.id))
