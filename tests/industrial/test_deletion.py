from datetime import timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.application import create_application
from app.auth.passwords import hash_password
from app.authz.policy import capture, recheck
from app.config import Settings
from app.db.models import (AuditEvent, Chunk, DocumentDeletion, DocumentRevision, FileObject, Job,
                           LegacyAlias, LogicalDocument, ParsedPage, User)
from app.jobs.gc import GarbageCollector
from app.jobs.parse_handler import ParseHandler
from app.jobs.repository import JobRepository
from app.parsing.worker import ParseResult
from app.services.deletion_service import DeletionService
from app.services.errors import DomainError
from app.services.legacy_resolver import resolve_legacy
from test_documents import fixture


def upload(service, admin, kb, name='deploy.md', key='upload-1'):
    result = service.upload(admin, kb, name, b'# Deploy\nPort 8088.', key, 'req_fixture')
    return UUID(result['document_id']), UUID(result['revision_id'])


def finish_parse(database, queue):
    lease = queue.claim('parse-fixture', ('parse',))
    parsed = ParseResult(filename='deploy.md', pages=['# Deploy\nPort 8088.'],
        chunks=[{'page': 1, 'start_line': 1, 'end_line': 2, 'heading': 'Deploy', 'text': '# Deploy\nPort 8088.'}])
    assert queue.finish(lease, lambda session, job: ParseHandler.persist(session, job, parsed)) == 'succeeded'


def expire(database, deletion_id):
    with database.transaction() as session:
        deletion = session.get(DocumentDeletion, deletion_id)
        deletion.deleted_at -= timedelta(days=8)
        deletion.restore_before -= timedelta(days=8)
        session.get(LogicalDocument, deletion.document_id).deleted_at = deletion.deleted_at
        job = session.scalar(select(Job).where(Job.type == 'gc', Job.idempotency_key == 'gc:' + str(deletion.id)))
        job.available_at = deletion.restore_before


def test_delete_revokes_snapshot_is_idempotent_and_stale_parse_cannot_revive(database, tmp_path):
    service, (admin, viewer, kb) = fixture(database, tmp_path)
    document, revision = upload(service, admin, kb)
    queue, deletions = JobRepository(database), DeletionService(database)
    old = queue.claim('old-worker', ('parse',))
    with database.transaction() as session:
        snapshot = capture(session, viewer, kb)
    with pytest.raises(DomainError, match='FORBIDDEN'):
        deletions.delete(viewer, document, 'req_denied')
    result = deletions.delete(admin, document, 'req_delete')
    assert deletions.delete(admin, document, 'req_repeat') == result
    assert queue.finish(old, lambda session, job: pytest.fail('Deleted parse wrote back')) == 'cancelled'
    with database.transaction() as session:
        with pytest.raises(DomainError, match='AUTHORIZATION_CHANGED'):
            recheck(session, snapshot)
        assert session.get(DocumentRevision, revision).status == 'deleted'
        assert session.scalar(select(func.count()).select_from(DocumentDeletion)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == 'document.delete')) == 1
    assert queue.claim('gc-too-early', ('gc',)) is None


def test_restore_admin_window_name_conflict_and_old_attempt_fencing(database, tmp_path):
    service, (admin, viewer, kb) = fixture(database, tmp_path)
    document, revision = upload(service, admin, kb)
    queue, deletions = JobRepository(database), DeletionService(database)
    old = queue.claim('old-worker', ('parse',))
    deletions.delete(admin, document, 'req_delete')
    with pytest.raises(DomainError, match='FORBIDDEN'):
        deletions.restore(viewer, document, 'req_denied')
    assert deletions.restore(admin, document, 'req_restore')['state'] == 'restored'
    assert queue.finish(old, lambda session, job: pytest.fail('Pre-deletion worker revived')) == 'cancelled'
    with database.transaction() as session:
        assert session.get(DocumentRevision, revision).status == 'uploaded'
    finish_parse(database, queue)
    result = deletions.delete(admin, document, 'req_delete_again')
    upload(service, admin, kb, key='new-live-document')
    with pytest.raises(DomainError, match='DOCUMENT_NAME_CONFLICT'):
        deletions.restore(admin, document, 'req_conflict')
    expire(database, UUID(result['deletion_id']))
    with pytest.raises(DomainError, match='RESTORE_WINDOW_EXPIRED'):
        deletions.restore(admin, document, 'req_late')


def test_gc_purges_only_deleted_content_keeps_shared_object_and_legacy_tombstone(database, tmp_path, monkeypatch):
    service, (admin, _, kb) = fixture(database, tmp_path)
    document, revision = upload(service, admin, kb)
    other_document, other_revision = upload(service, admin, kb, name='other.md', key='upload-2')
    queue, deletions = JobRepository(database), DeletionService(database)
    finish_parse(database, queue)
    finish_parse(database, queue)
    with database.transaction() as session:
        chunk = session.scalar(select(Chunk).where(Chunk.revision_id == revision))
        session.add(LegacyAlias(kind='chunk', legacy_id='legacy-deploy', kb_id=kb, target_id=chunk.id, revision_id=revision))
    first = deletions.delete(admin, document, 'req_delete')
    expire(database, UUID(first['deletion_id']))
    collector = GarbageCollector(database, service.objects)
    recycled = []
    monkeypatch.setattr(service.objects, 'recycle', lambda key: recycled.append(key))
    lease = queue.claim('gc-fixture', ('gc',))
    assert collector.run(queue, lease) == 'succeeded'
    assert recycled == []
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(Chunk)) == 1
        assert session.scalar(select(func.count()).select_from(ParsedPage)) == 1
        assert session.get(DocumentRevision, revision).file_object_id is None
        assert session.get(DocumentRevision, other_revision).file_object_id is not None
        assert resolve_legacy(session, 'chunk', 'legacy-deploy', authorized_kb_id=kb).status == 410
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 3
    second = deletions.delete(admin, other_document, 'req_delete_2')
    expire(database, UUID(second['deletion_id']))
    assert collector.run(queue, queue.claim('gc-fixture', ('gc',))) == 'succeeded'
    assert len(recycled) == 1
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(FileObject)) == 0


def test_gc_recycle_failure_is_durable_and_reuse_cancels_object_collection(database, tmp_path, monkeypatch):
    service, (admin, _, kb) = fixture(database, tmp_path)
    document, revision = upload(service, admin, kb)
    queue, deletions = JobRepository(database), DeletionService(database)
    result = deletions.delete(admin, document, 'req_delete')
    expire(database, UUID(result['deletion_id']))
    collector = GarbageCollector(database, service.objects)
    def unavailable(key):
        raise OSError('Synthetic trash failure')
    monkeypatch.setattr(service.objects, 'recycle', unavailable)
    lease = queue.claim('gc-fixture', ('gc',))
    assert collector.run(queue, lease) == 'retry_wait'
    with database.transaction() as session:
        assert session.get(DocumentRevision, revision).purged_at is not None
        file = session.scalar(select(FileObject))
        assert file.gc_pending_at is not None
        key = file.storage_key
    _, reused_revision = upload(service, admin, kb, name='reuse.md', key='reuse')
    collector.collect_object(key)
    with database.transaction() as session:
        assert session.get(DocumentRevision, reused_revision).file_object_id is not None
        assert session.scalar(select(FileObject)).gc_pending_at is None
    assert service.objects.path_for(key).is_file()


def test_http_delete_source_gone_and_restore_with_csrf(database, tmp_path):
    service, (admin, _, kb) = fixture(database, tmp_path)
    document, revision = upload(service, admin, kb)
    finish_parse(database, JobRepository(database))
    with database.transaction() as session:
        session.get(User, admin).password_hash = hash_password('synthetic-delete-password')
    app = create_application(Settings(data_dir=tmp_path, database_url=database.engine.url.render_as_string(hide_password=False)))
    with TestClient(app, base_url='https://localhost', headers={'Origin': 'https://localhost'}) as client:
        login = client.post('/api/v1/auth/login', json={'email': 'admin@example.invalid', 'password': 'synthetic-delete-password'})
        client.headers['X-CSRF-Token'] = login.headers['X-CSRF-Token']
        assert client.delete(f'/api/v1/documents/{document}').status_code == 200
        assert client.get(f'/api/v1/document-revisions/{revision}/source').status_code == 410
        assert client.post(f'/api/v1/documents/{document}/restore').status_code == 200
        assert client.get(f'/api/v1/document-revisions/{revision}/source').status_code == 200
