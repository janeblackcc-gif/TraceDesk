import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from app.api.schemas import QueryRequest
from app.application import create_application
from app.config import Settings
from app.db.models import Chunk, DocumentRevision, KnowledgeBase, User
from app.jobs.query_handler import QueryHandler
from app.jobs.repository import JobRepository
from app.operations.backup import PostgresTools, backup, restore, verify_backup
from app.services.deletion_service import DeletionService
from app.services.document_service import DocumentService
from app.services.errors import DomainError
from app.services.query_service import QueryService
from app.services.source_service import SourceService
from app.storage.object_store import ObjectStore
from conftest import disposable_database
from test_documents import fixture
from test_deletion import finish_parse, upload
from test_indexing import index


def pg_tools(database):
    return PostgresTools(database.engine.url, container=os.environ.get('TRACEDESK_BACKUP_TEST_CONTAINER'))


def test_maintenance_fences_transactions_and_http_then_releases(database, tmp_path):
    app = create_application(Settings(data_dir=tmp_path, database_url=database.engine.url.render_as_string(hide_password=False)))
    with TestClient(app, base_url='https://localhost', headers={'Origin': 'https://localhost'}) as client:
        with database.maintenance():
            assert client.get('/livez').status_code == 200
            assert client.get('/readyz').json()['code'] == 'MAINTENANCE_IN_PROGRESS'
            response = client.post('/api/v1/auth/login', json={'email': 'test@example.invalid', 'password': 'fixture'})
            assert response.status_code == 503
            assert response.json()['error']['code'] == 'MAINTENANCE_IN_PROGRESS'
            with pytest.raises(DomainError, match='MAINTENANCE_IN_PROGRESS'):
                with database.transaction():
                    pytest.fail('Transaction entered during backup')
        assert client.get('/readyz').status_code == 200
    with database.transaction():
        with pytest.raises(DomainError, match='DATABASE_BUSY'):
            with database.maintenance():
                pytest.fail('Maintenance ignored a transaction')


def test_backup_refuses_running_jobs_before_creating_files(database, tmp_path):
    documents, (admin, _, kb) = fixture(database, tmp_path)
    upload(documents, admin, kb)
    assert JobRepository(database).claim('running-parser', ('parse',))
    with pytest.raises(DomainError, match='BACKUP_JOBS_RUNNING'):
        backup(database, documents.objects, tmp_path / 'backup', pg_tools(database), app_ref='dev:fixture')
    assert not (tmp_path / 'backup').exists()


def test_real_backup_restore_preserves_acl_objects_indexes_legacy_ids_and_ten_queries(database, tmp_path):
    documents, (admin, viewer, kb) = fixture(database, tmp_path)
    document, revision = upload(documents, admin, kb)
    queue = JobRepository(database)
    finish_parse(database, queue)
    generation = index(database)
    with database.transaction() as session:
        session.get(DocumentRevision, revision).legacy_doc_id = 'frozen-legacy-document'
        session.scalar(select(Chunk)).legacy_chunk_id = 'frozen-legacy-chunk:1'
        outsider = User(email='outsider@example.invalid', display_name='Outsider')
        session.add(outsider)
        session.flush()
        outsider_id = outsider.id
    # Include a retained soft deletion and a future queued GC job in the snapshot.
    deleted = documents.upload(admin, kb, 'deleted.md', b'Retained original.', 'delete-fixture', 'req_fixture')
    from uuid import UUID
    DeletionService(database).delete(admin, UUID(deleted['document_id']), 'req_delete')
    queries, expected = QueryService(database), []
    for number in range(10):
        request = QueryRequest(knowledge_base_id=kb, question=f'Port 8088 {number}', method='bm25')
        accepted = queries.create(viewer, request, f'before-{number}', 'req_before')
        assert QueryHandler(database, lambda cancel: pytest.fail('Evidence called a model')).run(
            queue, queue.claim('evidence', ('query',))) == 'succeeded'
        result = queries.read(viewer, accepted.query_id)
        expected.append((accepted.query_id, request, result.status, [source.model_dump() for source in result.sources]))
    before_source = SourceService(database).read(viewer, revision, 1)
    archive = tmp_path / 'backup'
    manifest = backup(database, documents.objects, archive, pg_tools(database), app_ref='dev:backup-fixture')
    assert verify_backup(archive) == manifest
    with disposable_database() as recovered:
        root = tmp_path / 'recovered-objects'
        assert restore(recovered, root, archive, pg_tools(recovered)) == manifest
        assert recovered.readiness().ready
        with recovered.transaction() as session:
            assert session.get(KnowledgeBase, kb).active_index_generation_id == generation
            assert session.scalar(select(Chunk.legacy_chunk_id)) == 'frozen-legacy-chunk:1'
        assert SourceService(recovered).read(viewer, revision, 1) == before_source
        recovered_queries, recovered_queue = QueryService(recovered), JobRepository(recovered)
        for number, (query_id, request, status, sources) in enumerate(expected):
            restored_result = recovered_queries.read(viewer, query_id)
            assert restored_result.status == status
            assert [source.model_dump() for source in restored_result.sources] == sources
            accepted = recovered_queries.create(viewer, request, f'after-{number}', 'req_after')
            assert QueryHandler(recovered, lambda cancel: pytest.fail('Evidence called a model')).run(
                recovered_queue, recovered_queue.claim('evidence', ('query',))) == 'succeeded'
            result = recovered_queries.read(viewer, accepted.query_id)
            assert result.status == status and [source.model_dump() for source in result.sources] == sources
        with pytest.raises(DomainError, match='NOT_FOUND'):
            SourceService(recovered).read(outsider_id, revision, 1)
        with pytest.raises(DomainError, match='FORBIDDEN'):
            DocumentService(recovered, ObjectStore(root)).upload(viewer, kb, 'denied.md', b'denied', 'denied', 'req_denied')
        with pytest.raises(DomainError, match='RESTORE_REQUIRES_EMPTY_DATABASE'):
            restore(recovered, tmp_path / 'another-root', archive, pg_tools(recovered))
        assert not (tmp_path / 'another-root').exists()
    report = {'status': 'passed', 'fixture': 'synthetic', 'fixed_queries': 10, 'same_sources': 10,
              'tables_verified': len(manifest.tables), 'objects_verified': len(manifest.objects),
              'acl_checks': ['viewer_read', 'viewer_write_denied', 'outsider_hidden'], 'schema': manifest.schema_revision}
    output = Path(os.environ.get('TRACEDESK_TEST_ARTIFACT_DIR', str(tmp_path))) / 'restore-drill.json'
    output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    # An altered dump is rejected before any restoration writes.
    with (archive / 'database.dump').open('ab') as stream:
        stream.write(b'corrupt')
    with disposable_database() as recovered:
        with pytest.raises(DomainError, match='BACKUP_DUMP_MISMATCH'):
            restore(recovered, tmp_path / 'corrupt-target', archive, pg_tools(recovered))
        assert not (tmp_path / 'corrupt-target').exists()
        with recovered.engine.connect() as connection:
            assert inspect(connection).get_table_names() == []
