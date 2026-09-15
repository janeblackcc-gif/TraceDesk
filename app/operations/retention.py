"""Explicit operator retention; defaults preserve security metadata for 90 days."""
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import delete, func, insert, select, update

from app.db.models import AuditEvent, AuthRateLimit, Job, JobAttempt, QueryRun, QuerySource, TraceRecord, UserSession
from app.db.session import Database
from app.jobs.repository import TERMINAL
from app.services.errors import DomainError


@dataclass(frozen=True)
class RetentionPolicy:
    audit_days: int = 90
    query_days: int = 30
    attempts_days: int = 30

    def __post_init__(self) -> None:
        if self.audit_days < 90 or self.query_days < 30 or self.attempts_days < 30:
            raise ValueError('Shorter retention requires an approved policy change')


def retain(database: Database, policy: RetentionPolicy = RetentionPolicy(), *, apply: bool = False) -> dict[str, int | str]:
    with database.maintenance() as connection:
        if apply and connection.scalar(select(Job.id).where(Job.state == 'running').limit(1)):
            raise DomainError('RETENTION_JOBS_RUNNING', 409)
        now = connection.scalar(select(func.clock_timestamp()))
        queries = select(QueryRun.id).where(QueryRun.created_at < now - timedelta(days=policy.query_days),
            QueryRun.status.in_(['succeeded', 'failed', 'partial', 'cancelled', 'superseded']),
            QueryRun.scope['retention'].as_string().is_distinct_from('expired'),
            ~select(Job.id).where(Job.id == QueryRun.job_id, ~Job.state.in_(TERMINAL)).exists())
        audit_filter = AuditEvent.occurred_at < now - timedelta(days=policy.audit_days)
        attempt_filter = JobAttempt.finished_at < now - timedelta(days=policy.attempts_days)
        session_filter = UserSession.expires_at < now - timedelta(days=policy.query_days)
        rate_filter = AuthRateLimit.window_started_at < now - timedelta(days=1)
        report: dict[str, int | str] = {'mode': 'apply' if apply else 'dry-run', 'status': 'passed',
            'query_payloads': connection.scalar(select(func.count()).select_from(queries.subquery())),
            'audit_events': connection.scalar(select(func.count()).select_from(AuditEvent).where(audit_filter)),
            'job_attempts': connection.scalar(select(func.count()).select_from(JobAttempt).where(attempt_filter)),
            'expired_sessions': connection.scalar(select(func.count()).select_from(UserSession).where(session_filter)),
            'rate_buckets': connection.scalar(select(func.count()).select_from(AuthRateLimit).where(rate_filter))}
        if not apply:
            return report
        connection.execute(delete(QuerySource).where(QuerySource.query_run_id.in_(queries)))
        connection.execute(delete(TraceRecord).where(TraceRecord.query_run_id.in_(queries)))
        connection.execute(update(QueryRun).where(QueryRun.id.in_(queries)).values(question='', response=None,
            status='superseded', scope=QueryRun.scope.op('||')({'retention': 'expired'})))
        connection.execute(delete(AuditEvent).where(audit_filter))
        connection.execute(delete(JobAttempt).where(attempt_filter))
        connection.execute(delete(UserSession).where(session_filter))
        connection.execute(delete(AuthRateLimit).where(rate_filter))
        connection.execute(insert(AuditEvent).values(action='retention.apply', resource_type='system',
            outcome='allowed', request_id='op_' + uuid4().hex, details={}))
        return report
