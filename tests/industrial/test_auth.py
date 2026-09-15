from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.application import create_application
from app.authz.policy import capture, kb_access, recheck
from app.config import Settings
from app.db.models import AuditEvent, KnowledgeBase, User, UserSession, Workspace
from app.services.errors import DomainError

PASSWORD = 'synthetic-test-passphrase-42'
BOOTSTRAP = 'test-bootstrap-' + 'x' * 40


@pytest.fixture
def auth(database, tmp_path):
    settings = Settings(data_dir=tmp_path, database_url=database.engine.url.render_as_string(hide_password=False), bootstrap_token=BOOTSTRAP)
    app = create_application(settings)
    with TestClient(app, base_url='https://localhost', headers={'Origin': 'https://localhost'}) as client:
        yield client, app.state.auth


def bootstrap(client):
    response = client.post('/api/v1/auth/bootstrap', json={'token': BOOTSTRAP, 'email': 'admin@example.invalid',
        'password': PASSWORD, 'display_name': 'Synthetic administrator'})
    assert response.status_code == 201, response.text
    return UUID(response.json()['id'])


def login(client, email='admin@example.invalid', password=PASSWORD):
    response = client.post('/api/v1/auth/login', json={'email': email, 'password': password})
    assert response.status_code == 204, response.text
    client.headers['X-CSRF-Token'] = response.headers['X-CSRF-Token']
    return response


def test_bootstrap_once_secure_cookie_csrf_rotation_logout(auth, database):
    client, _ = auth
    bootstrap(client)
    response = client.post('/api/v1/auth/bootstrap', json={'token': BOOTSTRAP, 'email': 'other@example.invalid',
        'password': PASSWORD, 'display_name': 'No second bootstrap'})
    assert response.status_code == 409
    response = login(client)
    cookie = response.headers['set-cookie']
    assert all(value in cookie.lower() for value in ['secure', 'httponly', 'samesite=strict', 'path=/'])
    old_token = client.cookies.get('__Host-tracedesk-session')
    assert response.content == b''
    assert client.get('/api/v1/me').status_code == 200
    response = login(client)
    assert client.cookies.get('__Host-tracedesk-session') != old_token
    with database.transaction() as session:
        record = session.scalar(select(UserSession).where(UserSession.revoked_at.is_not(None)))
        assert record is not None
    assert client.post('/api/v1/auth/logout', headers={'X-CSRF-Token': 'wrong'}).status_code == 403
    assert client.post('/api/v1/auth/logout').status_code == 204
    assert client.get('/api/v1/me').status_code == 401


def test_login_failures_are_uniform_and_audited(auth, database):
    client, _ = auth
    bootstrap(client)
    codes = []
    for email in ['missing@example.invalid', 'admin@example.invalid']:
        response = client.post('/api/v1/auth/login', json={'email': email, 'password': 'wrong-password'})
        assert response.status_code == 401
        codes.append(response.json()['error']['code'])
    assert codes == ['INVALID_CREDENTIALS'] * 2
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == 'login', AuditEvent.outcome == 'denied')) == 2


def test_no_secret_in_validation_response_or_repr(auth, database, tmp_path):
    client, _ = auth
    secret = 'NeverEchoThisPassword_' + 'x' * 130
    response = client.post('/api/v1/auth/login', json={'email': 'bad', 'password': secret})
    assert response.status_code == 422 and secret not in response.text
    value = Settings(data_dir=tmp_path, database_url=database.engine.url.render_as_string(hide_password=False), bootstrap_token=BOOTSTRAP)
    assert BOOTSTRAP not in repr(value)
    assert database.engine.url.password not in repr(value)


def test_cross_site_and_oversized_auth_body_rejected(auth):
    client, _ = auth
    assert client.post('/api/v1/auth/login', headers={'Origin': 'https://evil.invalid'}, json={}).status_code == 403
    assert client.post('/api/v1/auth/login', content=b'x' * 17000).status_code == 413


def test_auth_rate_limit_persists_in_database(auth):
    client, _ = auth
    bootstrap(client)
    for _ in range(10):
        assert client.post('/api/v1/auth/login', json={'email': 'missing@example.invalid', 'password': PASSWORD}).status_code == 401
    response = client.post('/api/v1/auth/login', json={'email': 'missing@example.invalid', 'password': PASSWORD})
    assert response.status_code == 429 and response.headers['Retry-After'] == '60'


def test_password_change_and_disable_revoke_sessions(auth, database):
    client, service = auth
    admin_id = bootstrap(client)
    user_id = service.create_user(admin_id, 'viewer@example.invalid', PASSWORD, 'Viewer', 'req_test')
    result = service.login('viewer@example.invalid', PASSWORD, 'fixture', 'req_test')
    assert service.authenticate(result.token).user_id == user_id
    service.change_user(admin_id, user_id, 'req_test', password='different-synthetic-passphrase')
    with pytest.raises(DomainError, match='AUTHENTICATION_REQUIRED'):
        service.authenticate(result.token)
    result = service.login('viewer@example.invalid', 'different-synthetic-passphrase', 'fixture', 'req_test')
    service.change_user(admin_id, user_id, 'req_test', disabled=True)
    with pytest.raises(DomainError, match='AUTHENTICATION_REQUIRED'):
        service.authenticate(result.token)


def test_idle_session_expiration(auth, database):
    client, service = auth
    bootstrap(client)
    result = service.login('admin@example.invalid', PASSWORD, 'fixture', 'req_test')
    with database.transaction() as session:
        record = session.scalar(select(UserSession))
        record.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=31)
    with pytest.raises(DomainError, match='AUTHENTICATION_REQUIRED'):
        service.authenticate(result.token)


@pytest.mark.parametrize(('role', 'access', 'allowed'), [
    ('viewer', 'viewer', True), ('viewer', 'editor', False), ('viewer', 'admin', False),
    ('editor', 'viewer', True), ('editor', 'editor', True), ('editor', 'admin', False),
    ('none', 'viewer', False),
])
def test_role_matrix_and_epoch_recheck(auth, database, role, access, allowed):
    client, service = auth
    admin_id = bootstrap(client)
    user_id = service.create_user(admin_id, 'viewer@example.invalid', PASSWORD, 'Viewer', 'req_test')
    with database.transaction() as session:
        workspace_id = session.scalar(select(Workspace.id))
        kb = KnowledgeBase(workspace_id=workspace_id, name='Restricted', slug='restricted')
        session.add(kb)
        session.flush()
        kb_id = kb.id
    service.set_workspace_member(admin_id, workspace_id, user_id, 'member', 'req_test')
    if role != 'none':
        service.set_kb_member(admin_id, kb_id, user_id, role, 'req_test')
    with database.transaction() as session:
        if allowed:
            assert kb_access(session, user_id, kb_id, access).id == kb_id
        else:
            with pytest.raises(DomainError) as denied:
                kb_access(session, user_id, kb_id, access)
            assert denied.value.status in {403, 404}
    if role != 'none':
        with database.transaction() as session:
            snapshot = capture(session, user_id, kb_id)
        service.set_kb_member(admin_id, kb_id, user_id, None, 'req_test')
        with database.transaction() as session, pytest.raises(DomainError, match='AUTHORIZATION_CHANGED'):
            recheck(session, snapshot)


def test_api_cannot_enumerate_other_kbs(auth, database):
    client, service = auth
    admin_id = bootstrap(client)
    service.create_user(admin_id, 'viewer@example.invalid', PASSWORD, 'Viewer', 'req_test')
    with database.transaction() as session:
        workspace_id = session.scalar(select(Workspace.id))
        kb = KnowledgeBase(workspace_id=workspace_id, name='private-title-canary', slug='private')
        session.add(kb)
        session.flush()
        kb_id = kb.id
    login(client, 'viewer@example.invalid')
    response = client.get(f'/api/v1/knowledge-bases/{kb_id}')
    assert response.status_code == 404 and 'private-title-canary' not in response.text
    assert client.get('/api/v1/me').json()['knowledge_bases'] == []
    response = client.post('/api/v1/users', json={'email': 'created@example.invalid', 'password': PASSWORD, 'display_name': 'Forbidden'})
    assert response.status_code == 403
    with database.transaction() as session:
        assert session.scalar(select(User).where(User.email == 'created@example.invalid')) is None
        assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == 'access.denied')) == 2
