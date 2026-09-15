import json
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.application import create_application
from app.audit.service import audit
from app.auth.passwords import hash_password
from app.config import Settings
from app.db.models import KnowledgeBase, User, Workspace, WorkspaceMember
from app.observability.logging import LOGGER
from test_documents import fixture


def login(client, email, password):
    response = client.post('/api/v1/auth/login', json={'email': email, 'password': password})
    assert response.status_code == 204
    client.headers['X-CSRF-Token'] = response.headers['X-CSRF-Token']


def test_audit_metrics_permissions_pagination_and_no_payload_in_logs(database, tmp_path, caplog):
    documents, (admin, viewer, kb) = fixture(database, tmp_path)
    secret = 'SYNTHETIC_PASSWORD_DO_NOT_LOG_75319'
    with database.transaction() as session:
        for actor in (admin, viewer):
            session.get(User, actor).password_hash = hash_password(secret)
        workspace = session.get(KnowledgeBase, kb).workspace_id
        other = Workspace(name='Other', slug='other')
        session.add(other)
        session.flush()
        other_id = other.id
        audit(session, 'synthetic.other', actor=admin, request_id='req_other', workspace_id=other_id)
        audit(session, 'synthetic.local', actor=admin, request_id='req_local', workspace_id=workspace)
    app = create_application(Settings(data_dir=tmp_path, database_url=database.engine.url.render_as_string(hide_password=False)))
    LOGGER.addHandler(caplog.handler)
    try:
        with TestClient(app, base_url='https://localhost', headers={'Origin': 'https://localhost'}) as client:
            assert client.get('/api/v1/audit-events').status_code == 401
            login(client, 'viewer@example.invalid', secret)
            assert client.get('/api/v1/audit-events').status_code == 403
            assert client.get('/api/v1/audit-events', params={'workspace_id': str(workspace)}).status_code == 403
            assert client.get('/api/v1/operations/metrics').status_code == 403
            with database.transaction() as session:
                session.get(WorkspaceMember, (workspace, viewer)).role = 'admin'
            scoped = client.get('/api/v1/audit-events', params={'workspace_id': str(workspace)}).json()
            assert scoped['items'] and all(row['workspace_id'] == str(workspace) for row in scoped['items'])
            assert client.get('/api/v1/audit-events', params={'workspace_id': str(other_id)}).status_code == 404
            login(client, 'admin@example.invalid', secret)
            first = client.get('/api/v1/audit-events', params={'limit': 2}).json()
            second = client.get('/api/v1/audit-events', params={'limit': 2, 'before_id': first['next_before_id']}).json()
            assert {row['id'] for row in first['items']}.isdisjoint({row['id'] for row in second['items']})
            response = client.get('/api/v1/operations/metrics')
            assert response.status_code == 200
            assert 'tracedesk_http_requests_total' in response.text
            assert 'tracedesk_metrics_database_available 1.0' in response.text
            assert 'tracedesk_storage_free_bytes' in response.text
            # Neither unmatched URL material nor submitted credentials enter logs.
            client.get('/sensitive-question-75319', params={'question': 'sensitive-quote-75319'})
            client.post('/api/v1/auth/login', json={'email': 'bad@example.invalid', 'password': secret})
            app.state.telemetry.force_flush()
        text = '\n'.join(record.getMessage() for record in caplog.records if record.name == LOGGER.name)
        assert secret not in text and 'sensitive-question-75319' not in text and 'sensitive-quote-75319' not in text
        assert 'postgresql+' not in text and '__Host-tracedesk-session' not in text
        records = [json.loads(record.getMessage()) for record in caplog.records if record.name == LOGGER.name]
        assert any(record['event'] == 'span' for record in records)
        assert any(record['event'] == 'http.request' and 'trace_id' in record for record in records)
    finally:
        LOGGER.removeHandler(caplog.handler)


def test_storage_failure_affects_readiness_but_not_liveness(database, tmp_path):
    app = create_application(Settings(data_dir=tmp_path, database_url=database.engine.url.render_as_string(hide_password=False)))
    with TestClient(app, base_url='https://localhost') as client:
        with patch('app.observability.health.tempfile.TemporaryFile', side_effect=PermissionError('fixture')):
            response = client.get('/readyz')
            assert response.status_code == 503 and response.json()['code'] == 'STORAGE_UNAVAILABLE'
            assert client.get('/livez').status_code == 200
        assert client.get('/readyz').status_code == 200
