import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.api.schemas import QueryRequest
from app.application import create_application
from app.auth.passwords import hash_password
from app.config import Settings
from app.db.models import QueryRun, QuerySource, TraceRecord, User
from app.jobs.query_handler import QueryHandler
from app.jobs.repository import JobRepository
from app.services.auth_service import AuthService
from app.services.errors import DomainError
from app.services.query_service import QueryService
from test_documents import fixture
from test_deletion import finish_parse, upload
from test_indexing import Provider


class AnswerProvider(Provider):
    def generate(self, question, evidence):
        return {'abstain': False, 'claims': [{'text': 'Port 8088.', 'citations': [
            {'chunk_id': evidence[0]['id'], 'quote': evidence[0]['text']}]}]}


def setup(database, tmp_path):
    documents, (admin, viewer, kb) = fixture(database, tmp_path)
    document, revision = upload(documents, admin, kb)
    queue = JobRepository(database)
    finish_parse(database, queue)
    return QueryService(database), queue, admin, viewer, kb


def test_async_evidence_idempotency_trace_and_export(database, tmp_path):
    service, queue, admin, viewer, kb = setup(database, tmp_path)
    body = QueryRequest(knowledge_base_id=kb, question='Port 8088')
    accepted = service.create(viewer, body, 'query-1', 'req_query')
    repeated = service.create(viewer, body, 'query-1', 'req_repeat')
    assert repeated.query_id == accepted.query_id
    assert accepted.status == 'queued'
    with pytest.raises(DomainError, match='IDEMPOTENCY_CONFLICT'):
        service.create(viewer, body.model_copy(update={'question': 'different'}), 'query-1', 'req_repeat')
    lease = queue.claim('evidence', ('query',))
    assert QueryHandler(database, lambda cancel: pytest.fail('Evidence mode called a model')).run(queue, lease) == 'succeeded'
    result = service.read(viewer, accepted.query_id)
    assert result.status == 'evidence_found' and result.sources and result.claims
    assert service.trace(viewer, accepted.query_id)['status'] == 'succeeded'
    assert '8088' in service.export(viewer, accepted.query_id, 'req_export')


def test_revocation_while_model_runs_discards_all_result_content(database, tmp_path):
    service, queue, admin, viewer, kb = setup(database, tmp_path)
    accepted = service.create(viewer, QueryRequest(knowledge_base_id=kb, question='Port 8088',
        profile='ollama', method='bm25'), 'query-1', 'req_query')
    entered, release = threading.Event(), threading.Event()
    class Slow(AnswerProvider):
        def generate(self, question, evidence):
            entered.set()
            assert release.wait(timeout=20)
            return super().generate(question, evidence)
    lease = queue.claim('slow-query', ('query',))
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(QueryHandler(database, lambda cancel: Slow()).run, queue, lease)
        try:
            assert entered.wait(timeout=10)
            AuthService(database).set_kb_member(admin, kb, viewer, None, 'req_revoke')
        finally:
            release.set()
        assert pending.result(timeout=20) == 'failed'
    for operation in (lambda: service.read(viewer, accepted.query_id),
                      lambda: service.trace(viewer, accepted.query_id),
                      lambda: service.export(viewer, accepted.query_id, 'req_export')):
        with pytest.raises(DomainError, match='NOT_FOUND'):
            operation()
    with database.transaction() as session:
        row = session.get(QueryRun, accepted.query_id)
        assert row.response is None and row.question == '' and row.status == 'superseded'
        assert session.scalar(select(func.count()).select_from(QuerySource)) == 0
        assert session.scalar(select(func.count()).select_from(TraceRecord)) == 0


@pytest.mark.parametrize('mode,expected', [('offline', 'partial'), ('missing_fact', 'no_evidence')])
def test_partial_is_only_for_technical_failure(database, tmp_path, mode, expected):
    service, queue, admin, viewer, kb = setup(database, tmp_path)
    accepted = service.create(viewer, QueryRequest(knowledge_base_id=kb, question='Port 8088',
        profile='ollama', method='bm25'), 'query-1', 'req_query')
    class Result(AnswerProvider):
        def generate(self, question, evidence):
            if mode == 'offline':
                raise DomainError('MODEL_CONNECTION_FAILED', 503, retryable=True)
            return {'abstain': True, 'claims': []}
    assert QueryHandler(database, lambda cancel: Result()).run(queue, queue.claim('query', ('query',))) == 'succeeded'
    response = service.read(viewer, accepted.query_id)
    assert response.status == expected and not response.claims and response.sources
    if mode == 'offline':
        assert response.error_code == 'MODEL_CONNECTION_FAILED'


def test_queued_cancel_and_http_query_contract(database, tmp_path):
    service, queue, admin, viewer, kb = setup(database, tmp_path)
    with database.transaction() as session:
        session.get(User, viewer).password_hash = hash_password('synthetic-query-password')
    app = create_application(Settings(data_dir=tmp_path, database_url=database.engine.url.render_as_string(hide_password=False)))
    with TestClient(app, base_url='https://localhost', headers={'Origin': 'https://localhost'}) as client:
        login = client.post('/api/v1/auth/login', json={'email': 'viewer@example.invalid', 'password': 'synthetic-query-password'})
        client.headers['X-CSRF-Token'] = login.headers['X-CSRF-Token']
        response = client.post('/api/v1/queries', headers={'Idempotency-Key': 'http-query'},
            json={'knowledge_base_id': str(kb), 'question': 'Port 8088'})
        assert response.status_code == 202, response.text
        body = response.json()
        cancelled = client.post('/api/v1/jobs/' + body['query_job_id'] + '/cancel')
        assert cancelled.status_code == 200
        result = client.get('/api/v1/queries/' + body['query_id'])
        assert result.status_code == 200 and result.json()['status'] == 'cancelled'
        assert not result.json()['sources']
        assert client.get('/api/v1/queries/' + body['query_id'] + '/export.md').status_code == 409
