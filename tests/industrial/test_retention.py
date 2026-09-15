from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.api.schemas import QueryRequest
from app.audit.service import audit
from app.db.models import AuditEvent, QueryRun, TraceRecord
from app.jobs.query_handler import QueryHandler
from app.operations.retention import RetentionPolicy, retain
from app.services.errors import DomainError
from test_queries import setup


def test_retention_previews_and_expires_only_old_terminal_payloads(database, tmp_path):
    queries, queue, admin, viewer, kb = setup(database, tmp_path)
    body = QueryRequest(knowledge_base_id=kb, question='Port 8088')
    old = queries.create(viewer, body, 'old-query', 'req_old')
    assert QueryHandler(database, lambda cancel: pytest.fail('Evidence called model')).run(
        queue, queue.claim('evidence', ('query',))) == 'succeeded'
    queued = queries.create(viewer, body, 'queued-query', 'req_queued')
    with database.transaction() as session:
        now = session.scalar(select(func.clock_timestamp()))
        session.get(QueryRun, old.query_id).created_at = now - timedelta(days=31)
        session.get(QueryRun, queued.query_id).created_at = now - timedelta(days=31)
        audit(session, 'old.audit', actor=admin, request_id='req_old_audit')
        session.scalar(select(AuditEvent).where(AuditEvent.action == 'old.audit')).occurred_at = now - timedelta(days=91)
    plan = retain(database)
    assert plan['query_payloads'] == 1 and plan['audit_events'] == 1
    assert queries.read(viewer, old.query_id).sources
    result = retain(database, apply=True)
    assert result['query_payloads'] == 1
    with pytest.raises(DomainError, match='QUERY_RESULT_EXPIRED'):
        queries.read(viewer, old.query_id)
    with pytest.raises(DomainError, match='QUERY_RESULT_EXPIRED'):
        queries.trace(viewer, old.query_id)
    assert queries.read(viewer, queued.query_id).status == 'queued'
    with database.transaction() as session:
        assert session.get(QueryRun, old.query_id).question == ''
        assert session.get(QueryRun, queued.query_id).question == 'Port 8088'
        assert session.scalar(select(func.count()).select_from(TraceRecord)) == 0
        assert session.scalar(select(AuditEvent.id).where(AuditEvent.action == 'old.audit')) is None
        assert session.scalar(select(AuditEvent.id).where(AuditEvent.action == 'retention.apply')) is not None
    assert retain(database)['query_payloads'] == 0
    with pytest.raises(ValueError):
        RetentionPolicy(audit_days=1)
