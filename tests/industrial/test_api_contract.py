
from fastapi.testclient import TestClient

from app.application import create_application
from app.auth.passwords import hash_password
from app.config import Settings
from app.db.models import KBMember, User
from test_documents import fixture
from test_deletion import finish_parse, upload
from app.jobs.repository import JobRepository


def test_every_team_route_has_session_security_and_read_models(tmp_path):
    app = create_application(Settings(data_dir=tmp_path, database_url='postgresql+psycopg://fixture@localhost/fixture'))
    schema = app.openapi()
    public = {'/api/v1/auth/login', '/api/v1/auth/bootstrap'}
    for path, operations in schema['paths'].items():
        if path.startswith('/api/') and path not in public:
            for operation in operations.values():
                assert operation.get('security') == [{'SessionCookie': []}], path
    assert schema['components']['securitySchemes']['SessionCookie']['in'] == 'cookie'
    paths = set(schema['paths'])
    assert {'/api/v1/queries/{query_id}/trace', '/api/v1/jobs', '/api/v1/documents/{document_id}/restore'} <= paths


def test_pagination_reparse_raw_file_and_authenticated_legacy(database, tmp_path):
    service, (admin, viewer, kb) = fixture(database, tmp_path)
    first_document, first_revision = upload(service, admin, kb)
    second_document, _ = upload(service, admin, kb, 'second.md', 'second-upload')
    queue = JobRepository(database)
    finish_parse(database, queue)
    finish_parse(database, queue)
    password = 'synthetic-contract-password'
    with database.transaction() as session:
        session.get(User, admin).password_hash = hash_password(password)
        session.get(User, viewer).password_hash = hash_password(password)
    app = create_application(Settings(data_dir=tmp_path, database_url=database.engine.url.render_as_string(hide_password=False)))
    with TestClient(app, base_url='https://localhost', headers={'Origin': 'https://localhost'}) as client:
        assert client.get('/api/library').status_code == 401
        login = client.post('/api/v1/auth/login', json={'email': 'admin@example.invalid', 'password': password})
        client.headers['X-CSRF-Token'] = login.headers['X-CSRF-Token']
        page = client.get(f'/api/v1/knowledge-bases/{kb}/documents?limit=1').json()
        second = client.get(f'/api/v1/knowledge-bases/{kb}/documents?limit=1&cursor=' + page['next_cursor']).json()
        assert {page['items'][0]['id'], second['items'][0]['id']} == {str(first_document), str(second_document)}
        assert second['next_cursor'] is None
        assert client.get(f'/api/v1/knowledge-bases/{kb}/documents?limit=201').status_code == 422
        assert client.get(f'/api/v1/document-revisions/{first_revision}/file').content == b'# Deploy\nPort 8088.'
        legacy = client.get('/api/sources/' + str(first_revision))
        assert legacy.status_code == 200 and legacy.json()['lines'] == ['# Deploy', 'Port 8088.']
        assert legacy.headers['deprecation'] == 'true'
        assert client.post('/api/ask', json={}).json()['error']['code'] == 'CLIENT_UPGRADE_REQUIRED'
        short_key = 'x' * 100
        one = client.post(f'/api/v1/document-revisions/{first_revision}/reparse', headers={'Idempotency-Key': short_key + '1'})
        two = client.post(f'/api/v1/document-revisions/{first_revision}/reparse', headers={'Idempotency-Key': short_key + '2'})
        assert one.status_code == two.status_code == 202
        assert one.json()['job_id'] != two.json()['job_id']
        repeated = client.post(f'/api/v1/document-revisions/{first_revision}/reparse', headers={'Idempotency-Key': short_key + '2'})
        assert repeated.json()['job_id'] == two.json()['job_id']
        login = client.post('/api/v1/auth/login', json={'email': 'viewer@example.invalid', 'password': password})
        client.headers['X-CSRF-Token'] = login.headers['X-CSRF-Token']
        assert client.post(f'/api/v1/document-revisions/{first_revision}/reparse', headers={'Idempotency-Key': 'denied'}).status_code == 403
        with database.transaction() as session:
            session.delete(session.get(KBMember, (kb, viewer)))
        for path in (f'/api/v1/document-revisions/{first_revision}/file', '/api/sources/' + str(first_revision),
                     f'/api/v1/knowledge-bases/{kb}/documents', f'/api/v1/jobs?kb_id={kb}'):
            assert client.get(path).status_code == 404
