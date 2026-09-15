from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Callable
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.db.models import (DocumentRevision, Job, JobAttempt, JsonValue, KnowledgeBase, LogicalDocument)
from app.db.session import Database
from app.services.errors import DomainError

TERMINAL = {'succeeded', 'failed', 'cancelled', 'superseded'}
KINDS = {'parse', 'index', 'query', 'gc', 'eval', 'reindex'}


class Superseded(Exception):
    """The pinned input changed before final publication."""


def payload_hash(payload: dict[str, JsonValue]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class Lease:
    job_id: UUID
    owner: str
    attempt: int
    type: str
    kb_id: UUID
    revision_id: UUID | None
    generation_id: UUID | None
    payload: dict[str, JsonValue]
    created_by: UUID


def enqueue(session: Session, *, kind: str, workspace_id: UUID, kb_id: UUID, created_by: UUID,
            key: str, payload: dict[str, JsonValue], revision_id: UUID | None = None,
            generation_id: UUID | None = None, completing_job: Job | None = None) -> Job:
    if kind not in KINDS or not 1 <= len(key) <= 200:
        raise DomainError('INVALID_JOB', 422)
    # Serialize quota + idempotency decisions across all API processes.
    session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('job-admission', 0))"))
    digest = payload_hash(payload)
    existing = session.scalar(select(Job).where(Job.type == kind, Job.idempotency_key == key))
    if existing is not None:
        if (existing.payload_hash != digest or existing.created_by != created_by or existing.kb_id != kb_id or
                existing.workspace_id != workspace_id or existing.revision_id != revision_id or existing.generation_id != generation_id):
            raise DomainError('IDEMPOTENCY_CONFLICT', 409)
        return existing
    active = Job.state.in_(['queued', 'running', 'retry_wait'])
    if completing_job is not None:
        # Only worker finalization uses this: completion and admission commit in
        # one transaction, so a saturated parse queue can hand off its slot.
        if completing_job.state != 'running' or completing_job.kb_id != kb_id:
            raise DomainError('INVALID_JOB_HANDOFF', 409)
        active = active & (Job.id != completing_job.id)
    if kind == 'query':
        global_count = session.scalar(select(func.count()).select_from(Job).where(active, Job.type == 'query'))
        user_count = session.scalar(select(func.count()).select_from(Job).where(active, Job.type == 'query', Job.created_by == created_by))
        if global_count >= 20 or user_count >= 2:
            raise DomainError('QUEUE_FULL', 429, retryable=True)
    elif kind in {'parse', 'index', 'reindex'}:
        count = session.scalar(select(func.count()).select_from(Job).where(active, Job.type.in_(['parse', 'index', 'reindex'])))
        if count >= 100:
            raise DomainError('QUEUE_FULL', 429, retryable=True)
        if kind in {'index', 'reindex'}:
            count = session.scalar(select(func.count()).select_from(Job).where(active, Job.type.in_(['index', 'reindex']), Job.kb_id == kb_id))
            if count:
                raise DomainError('INDEX_ALREADY_QUEUED', 409, retryable=True)
    job = Job(type=kind, workspace_id=workspace_id, kb_id=kb_id, created_by=created_by,
              idempotency_key=key, payload=payload, payload_hash=digest, revision_id=revision_id, generation_id=generation_id)
    session.add(job)
    session.flush()
    session.execute(text("SELECT pg_notify('tracedesk_jobs', '')"))
    return job


class JobRepository:
    def __init__(self, database: Database, *, lease_seconds: int = 30):
        if lease_seconds < 1:
            raise ValueError('lease_seconds must be positive')
        self.database = database
        self.lease_seconds = lease_seconds

    def claim(self, owner: str, kinds: tuple[str, ...]) -> Lease | None:
        if not owner or len(owner) > 200 or not kinds or not set(kinds) <= KINDS:
            raise ValueError('Invalid worker identity or kinds')
        with self.database.transaction() as session:
            # Admission limits must also cover separate worker processes.
            session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('job-claim-capacity', 0))"))
            now = session.scalar(select(func.clock_timestamp()))
            running = dict(session.execute(select(Job.type, func.count()).where(Job.state == 'running',
                Job.lease_expires_at > now).group_by(Job.type)).all())
            eligible = [kind for kind in kinds if (
                running.get('index', 0) + running.get('reindex', 0) if kind in {'index', 'reindex'} else running.get(kind, 0)
            ) < (2 if kind == 'parse' else 1)]
            if not eligible:
                return None
            job = session.scalar(select(Job).where(Job.state == 'queued', Job.available_at <= now,
                Job.type.in_(eligible), Job.attempt_count < Job.max_attempts).order_by(Job.priority.desc(), Job.created_at, Job.id)
                .with_for_update(skip_locked=True).limit(1))
            if job is None:
                return None
            if job.cancel_requested_at is not None:
                job.state, job.finished_at = 'cancelled', now
                return None
            job.state, job.lease_owner = 'running', owner
            job.attempt_count += 1
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            session.add(JobAttempt(job_id=job.id, attempt_no=job.attempt_count, worker_id=owner, started_at=now))
            return Lease(job.id, owner, job.attempt_count, job.type, job.kb_id, job.revision_id,
                         job.generation_id, dict(job.payload), job.created_by)

    def _owned(self, session: Session, lease: Lease) -> Job | None:
        job = session.get(Job, lease.job_id, with_for_update=True, populate_existing=True)
        now = session.scalar(select(func.clock_timestamp()))
        if (job is None or job.state != 'running' or job.lease_owner != lease.owner or
                job.attempt_count != lease.attempt or job.lease_expires_at is None or job.lease_expires_at <= now):
            return None
        return job

    def heartbeat(self, lease: Lease) -> bool:
        with self.database.transaction() as session:
            job = self._owned(session, lease)
            if job is None or job.cancel_requested_at is not None:
                return False
            now = session.scalar(select(func.clock_timestamp()))
            job.heartbeat_at = now
            job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
            return True

    def cancel(self, job_id: UUID, authorize: Callable[[Session, Job], None] | None = None) -> str:
        # Authorization belongs to the calling service, never to worker identity.
        with self.database.transaction() as session:
            job = session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise DomainError('NOT_FOUND', 404)
            if authorize is not None:
                authorize(session, job)
            if job.state in TERMINAL:
                return job.state
            now = session.scalar(select(func.clock_timestamp()))
            job.cancel_requested_at = job.cancel_requested_at or now
            if job.state in {'queued', 'retry_wait'}:
                self._terminal(session, job, 'cancelled')
            return job.state

    @staticmethod
    def _terminal(session: Session, job: Job, state: str, error: str | None = None) -> None:
        now = session.scalar(select(func.clock_timestamp()))
        job.state = state
        job.error_code = error
        job.finished_at = now
        job.lease_owner = None
        job.lease_expires_at = None
        attempt = session.scalar(select(JobAttempt).where(JobAttempt.job_id == job.id, JobAttempt.attempt_no == job.attempt_count))
        if attempt:
            attempt.finished_at, attempt.outcome, attempt.error_code = now, state, error
        if job.type == 'query' and state in {'cancelled', 'superseded', 'failed'}:
            from app.services.query_service import QueryService
            QueryService.redact(session, job)

    def finish(self, lease: Lease, effect: Callable[[Session, Job], dict[str, JsonValue]]) -> str:
        """Validate ownership and lifecycle, then commit result and effects together."""
        with self.database.transaction() as session:
            session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                            {'key': 'kb-mutation:' + str(lease.kb_id)})
            job = self._owned(session, lease)
            if job is None:
                return 'lease_lost'
            if job.cancel_requested_at is not None:
                self._terminal(session, job, 'cancelled')
                return 'cancelled'
            kb = session.get(KnowledgeBase, job.kb_id, with_for_update=True)
            if kb is None or kb.deleted_at is not None:
                self._terminal(session, job, 'superseded')
                return 'superseded'
            if job.revision_id is not None and job.type != 'gc':
                revision = session.get(DocumentRevision, job.revision_id)
                document = session.get(LogicalDocument, revision.document_id, with_for_update=True) if revision else None
                if (revision is None or revision.deleted_at is not None or document is None or
                        document.kb_id != job.kb_id or document.deleted_at is not None or document.desired_revision_id != revision.id):
                    self._terminal(session, job, 'superseded')
                    return 'superseded'
            try:
                with session.begin_nested():
                    job.result = effect(session, job)
            except Superseded:
                self._terminal(session, job, 'superseded', 'INPUT_CHANGED')
                return 'superseded'
            self._terminal(session, job, 'succeeded')
            return 'succeeded'

    def fail(self, lease: Lease, code: str, *, retryable: bool = False,
             effect: Callable[[Session, Job], None] | None = None) -> str:
        if not code or len(code) > 100 or not code.replace('_', '').isalnum():
            raise ValueError('Error code must be a bounded identifier, never exception text')
        with self.database.transaction() as session:
            session.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))'),
                            {'key': 'kb-mutation:' + str(lease.kb_id)})
            job = self._owned(session, lease)
            if job is None:
                return 'lease_lost'
            if job.cancel_requested_at is not None:
                self._terminal(session, job, 'cancelled')
            elif retryable and job.attempt_count < job.max_attempts:
                self._terminal(session, job, 'retry_wait', code)
                job.finished_at = None
                job.available_at = session.scalar(select(func.clock_timestamp())) + timedelta(seconds=5 if job.attempt_count == 1 else 30)
            else:
                self._terminal(session, job, 'failed', code)
            if effect is not None:
                effect(session, job)
            return job.state

    def recover(self) -> int:
        changed = 0
        with self.database.transaction() as session:
            now = session.scalar(select(func.clock_timestamp()))
            jobs = session.scalars(select(Job).where(
                ((Job.state == 'running') & (Job.lease_expires_at <= now)) |
                ((Job.state == 'retry_wait') & (Job.available_at <= now)))
                .with_for_update(skip_locked=True).limit(100)).all()
            for job in jobs:
                if job.cancel_requested_at is not None:
                    self._terminal(session, job, 'cancelled')
                elif job.state == 'running':
                    exhausted = job.attempt_count >= job.max_attempts
                    self._terminal(session, job, 'failed' if exhausted else 'queued', 'LEASE_EXPIRED')
                    if not exhausted:
                        job.finished_at, job.available_at = None, now
                else:
                    job.state = 'queued'
                changed += 1
        return changed
