import os
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.db.models import Chunk, DocumentRevision, FileObject, Job, KnowledgeBase, LogicalDocument, ParseRun, User, Workspace, WorkspaceMember, KBMember
from app.jobs.parse_handler import ParseHandler
from app.jobs.repository import JobRepository
from app.parsing.worker import ParseResult
from app.services.document_service import DocumentService
from app.services.errors import DomainError
from app.storage.object_store import ObjectStore
from app.application import create_application
from app.auth.passwords import hash_password
from app.config import Settings
from app.parsing.worker import DockerParser


def fixture(database, tmp_path):
    with database.transaction() as session:
        admin = User(email='admin@example.invalid', display_name='Admin', is_system_admin=True)
        viewer = User(email='viewer@example.invalid', display_name='Viewer')
        workspace = Workspace(name='Fixture', slug='fixture')
        session.add_all([admin, viewer, workspace])
        session.flush()
        kb = KnowledgeBase(workspace_id=workspace.id, name='KB', slug='kb')
        session.add(kb)
        session.flush()
        session.add_all([WorkspaceMember(workspace_id=workspace.id, user_id=viewer.id, role='member'),
                         KBMember(kb_id=kb.id, user_id=viewer.id, role='viewer')])
        identifiers = admin.id, viewer.id, kb.id
    return DocumentService(database, ObjectStore(tmp_path / 'objects')), identifiers


def test_duplicate_and_receipt_conflict(database, tmp_path):
    service, (admin, _, kb) = fixture(database, tmp_path)
    first = service.upload(admin, kb, 'deploy.md', b'# Deploy\nPort 8088.', 'request-1', 'req_fixture')
    assert service.upload(admin, kb, 'deploy.md', b'# Deploy\nPort 8088.', 'request-1', 'req_fixture') == first
    second = service.upload(admin, kb, 'deploy.md', b'# Deploy\nPort 8088.', 'request-2', 'req_fixture')
    assert second['revision_id'] == first['revision_id'] and second['duplicate'] is True
    with pytest.raises(DomainError, match='IDEMPOTENCY_CONFLICT'):
        service.upload(admin, kb, 'deploy.md', b'Changed', 'request-1', 'req_fixture')
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(DocumentRevision)) == 1
        assert session.scalar(select(func.count()).select_from(FileObject)) == 1
        assert session.scalar(select(func.count()).select_from(Job)) == 1


def test_viewer_and_path_rejection_have_no_side_effect(database, tmp_path):
    service, (admin, viewer, kb) = fixture(database, tmp_path)
    with pytest.raises(DomainError, match='FORBIDDEN'):
        service.upload(viewer, kb, 'deploy.md', b'content', 'request-1', 'req_fixture')
    for name in ['../../deploy.md', 'CON.txt', 'path\\evil.md', 'file.exe']:
        with pytest.raises(DomainError):
            service.upload(admin, kb, name, b'content', 'request-1', 'req_fixture')
    assert list((service.objects.root / 'sha256').glob('*/*')) == []
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(LogicalDocument)) == 0


def test_replacement_keeps_old_active_and_supersedes_old_parse(database, tmp_path):
    service, (admin, _, kb) = fixture(database, tmp_path)
    first = service.upload(admin, kb, 'deploy.md', b'# Deploy\nPort 8088.', 'request-1', 'req_fixture')
    queue = JobRepository(database)
    old = queue.claim('worker', ('parse',))
    with database.transaction() as session:
        document = session.get(LogicalDocument, UUID(first['document_id']))
        document.active_revision_id = UUID(first['revision_id'])
    second = service.upload(admin, kb, 'deploy.md', b'# Deploy\nPort 8099.', 'request-2', 'req_fixture')
    assert second['revision_id'] != first['revision_id']
    assert queue.finish(old, lambda session, job: pytest.fail('stale parse committed')) == 'superseded'
    with database.transaction() as session:
        document = session.get(LogicalDocument, UUID(first['document_id']))
        assert str(document.active_revision_id) == first['revision_id']
        assert str(document.desired_revision_id) == second['revision_id']


def test_parse_result_commits_atomically_and_retains_raw_file(database, tmp_path):
    service, (admin, _, kb) = fixture(database, tmp_path)
    result = service.upload(admin, kb, 'deploy.md', b'# Deploy\nPort 8088.', 'request-1', 'req_fixture')
    queue = JobRepository(database)
    lease = queue.claim('worker', ('parse',))
    parsed = ParseResult(filename='deploy.md', pages=['# Deploy\nPort 8088.'],
        chunks=[{'page': 1, 'start_line': 1, 'end_line': 2, 'heading': 'Deploy', 'text': '# Deploy\nPort 8088.'}])
    assert queue.finish(lease, lambda session, job: ParseHandler.persist(session, job, parsed)) == 'succeeded'
    with database.transaction() as session:
        revision = session.get(DocumentRevision, UUID(result['revision_id']))
        assert revision.status == 'parsed' and revision.source_file_available
        assert session.scalar(select(func.count()).select_from(Chunk)) == 1
        item = session.get(FileObject, revision.file_object_id)
        assert service.objects.path_for(item.storage_key).read_bytes() == b'# Deploy\nPort 8088.'


def test_http_upload_worker_container_and_authorized_source(database, tmp_path):
    image = os.environ.get('TRACEDESK_PARSER_TEST_IMAGE')
    if not image:
        pytest.skip('Docker parser integration requires TRACEDESK_PARSER_TEST_IMAGE')
    service, (admin, viewer, kb) = fixture(database, tmp_path)
    password = 'synthetic-documents-password'
    with database.transaction() as session:
        session.get(User, admin).password_hash = hash_password(password)
        session.get(User, viewer).password_hash = hash_password(password)
    app = create_application(Settings(data_dir=tmp_path, database_url=database.engine.url.render_as_string(hide_password=False)))
    with TestClient(app, base_url='https://localhost', headers={'Origin': 'https://localhost'}) as client:
        login = client.post('/api/v1/auth/login', json={'email': 'admin@example.invalid', 'password': password})
        assert login.status_code == 204
        client.headers['X-CSRF-Token'] = login.headers['X-CSRF-Token']
        response = client.post(f'/api/v1/knowledge-bases/{kb}/documents', files={'file': ('deploy.md', b'# Deploy\nPort 8088.')}, headers={'Idempotency-Key': 'http-1'})
        assert response.status_code == 202, response.text
        revision_id, job_id = response.json()['revision_id'], response.json()['job_id']
        if os.name != 'nt':
            # Production workers create objects as UID 65532, the same identity used
            # by the parser container. GitHub's runner creates this synthetic object
            # as its host UID, so grant read-only access without weakening the parser.
            source_path = next((service.objects.root / 'sha256').glob('*/*'))
            source_path.chmod(0o644)
        queue = JobRepository(database)
        lease = queue.claim('http-fixture-worker', ('parse',))
        assert ParseHandler(database, service.objects, DockerParser(image, timeout=30)).run(queue, lease) == 'succeeded'
        assert client.get('/api/v1/jobs/' + job_id).json()['state'] == 'succeeded'
        source = client.get(f'/api/v1/document-revisions/{revision_id}/source')
        assert source.status_code == 200 and source.json()['text'] == '# Deploy\nPort 8088.'
        started = threading.Event()

        class HungParser(DockerParser):
            def command(self, path, filename, name):
                command = super().command(path, filename, name)
                started.set()
                return command[:-2] + ['--entrypoint', 'python', self.image, '-c', 'import time; time.sleep(300)']

        input_path = tmp_path / 'hung-fixture.txt'
        input_path.write_bytes(b'fixture')
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(HungParser(image, timeout=3).parse, input_path, 'fixture.txt')
            assert started.wait(timeout=5)
            assert client.get('/livez').status_code == 200
            assert client.get(f'/api/v1/document-revisions/{revision_id}/source').status_code == 200
            with pytest.raises(DomainError, match='PARSE_TIMEOUT'):
                pending.result(timeout=30)
        login = client.post('/api/v1/auth/login', json={'email': 'viewer@example.invalid', 'password': password})
        client.headers['X-CSRF-Token'] = login.headers['X-CSRF-Token']
        assert client.get(f'/api/v1/document-revisions/{revision_id}/source').status_code == 200
        denied = client.post(f'/api/v1/knowledge-bases/{kb}/documents', files={'file': ('forbidden.md', b'no-write')}, headers={'Idempotency-Key': 'http-2'})
        assert denied.status_code == 403
        with database.transaction() as session:
            session.delete(session.get(KBMember, (kb, viewer)))
        denied = client.get(f'/api/v1/document-revisions/{revision_id}/source')
        assert denied.status_code == 404 and '8088' not in denied.text


def test_failed_parse_records_job_revision_and_attempt(database, tmp_path):
    service, (admin, _, kb) = fixture(database, tmp_path)
    result = service.upload(admin, kb, 'invalid.pdf', b'%PDF-invalid', 'invalid-1', 'req_fixture')
    queue = JobRepository(database)
    lease = queue.claim('worker', ('parse',))

    class RejectedParser(DockerParser):
        def parse(self, path, filename, cancel=None):
            raise DomainError('INPUT_INVALID', 422)

    assert ParseHandler(database, service.objects, RejectedParser('unused-fixture')).run(queue, lease) == 'failed'
    with database.transaction() as session:
        assert session.get(Job, UUID(result['job_id'])).error_code == 'INPUT_INVALID'
        assert session.get(DocumentRevision, UUID(result['revision_id'])).status == 'failed'
        run = session.scalar(select(ParseRun))
        assert run.status == 'failed' and run.error_code == 'INPUT_INVALID'
