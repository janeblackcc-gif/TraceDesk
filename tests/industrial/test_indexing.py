import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select

from app.db.models import ChunkEmbedding, DocumentRevision, IndexGeneration, KnowledgeBase, LogicalDocument
from app.jobs.index_handler import IndexHandler
from app.jobs.repository import JobRepository
from app.models.profile import EmbeddingIdentity
from app.rag.sparse import candidates, dense_scores, retrieve
from app.retrieval import search_context
from app.services.deletion_service import DeletionService
from app.services.index_service import IndexService
from test_documents import fixture
from test_deletion import finish_parse, upload


class Provider:
    def identity(self):
        return EmbeddingIdentity('fixture', 'fixture', 'a' * 64)
    def embed(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]


def index(database):
    queue = JobRepository(database)
    lease = queue.claim('index-fixture', ('index', 'reindex'))
    assert lease is not None
    assert IndexHandler(database, lambda cancel: Provider()).run(queue, lease) == 'succeeded'
    with database.transaction() as session:
        return session.scalar(select(IndexGeneration.id).where(IndexGeneration.status == 'active'))


def test_automatic_parse_index_activation_and_postgres_retrieval(database, tmp_path):
    service, (admin, _, kb) = fixture(database, tmp_path)
    document, revision = upload(service, admin, kb)
    finish_parse(database, JobRepository(database))
    generation = index(database)
    with database.transaction() as session:
        assert session.get(LogicalDocument, document).active_revision_id == revision
        assert session.get(DocumentRevision, revision).status == 'ready'
        assert session.get(KnowledgeBase, kb).active_index_generation_id == generation
        rows = candidates(session, kb, generation)
        assert len(rows) == 1
        vector = [1.0] + [0.0] * 1023
        assert dense_scores(session, kb, generation, vector)[rows[0]['id']] == pytest.approx(1.0)
        actual = retrieve(session, kb, generation, ['Port 8088'], vectors=[vector])
        expected = search_context(['Port 8088'], rows, vectors={row['id']: vector for row in rows}, query_vectors=[vector])
        assert [row['id'] for row in actual] == [row['id'] for row in expected]
        assert [row['score'] for row in actual] == [row['score'] for row in expected]


def test_replacement_during_embedding_keeps_active_and_fences_old_worker(database, tmp_path):
    service, (admin, _, kb) = fixture(database, tmp_path)
    document, revision = upload(service, admin, kb)
    queue = JobRepository(database)
    finish_parse(database, queue)
    first = index(database)
    service.upload(admin, kb, 'deploy.md', b'# Deploy\nPort 8099.', 'replace-1', 'req_fixture')
    finish_parse(database, queue)
    lease = queue.claim('slow-index', ('reindex', 'index'))
    started, release = threading.Event(), threading.Event()
    class Slow(Provider):
        def embed(self, texts):
            started.set()
            assert release.wait(timeout=20)
            return super().embed(texts)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(IndexHandler(database, lambda cancel: Slow()).run, queue, lease)
        try:
            assert started.wait(timeout=10)
            service.upload(admin, kb, 'deploy.md', b'# Deploy\nPort 9000.', 'replace-2', 'req_fixture')
            with database.transaction() as session:
                assert session.get(KnowledgeBase, kb).active_index_generation_id == first
                assert session.get(LogicalDocument, document).active_revision_id == revision
        finally:
            release.set()
        assert pending.result(timeout=20) == 'superseded'
    with database.transaction() as session:
        assert session.scalar(select(func.count()).select_from(ChunkEmbedding)) == 1


def test_digest_change_and_delete_never_activate_failed_generation(database, tmp_path):
    service, (admin, _, kb) = fixture(database, tmp_path)
    document, _ = upload(service, admin, kb)
    queue = JobRepository(database)
    finish_parse(database, queue)
    class Changed(Provider):
        changed = False
        def identity(self):
            return EmbeddingIdentity('fixture', 'fixture', ('b' if self.changed else 'a') * 64)
        def embed(self, texts):
            self.changed = True
            return super().embed(texts)
    assert IndexHandler(database, lambda cancel: Changed()).run(queue, queue.claim('changed', ('reindex',))) == 'failed'
    with database.transaction() as session:
        assert session.get(KnowledgeBase, kb).active_index_generation_id is None
        assert session.scalar(select(func.count()).select_from(ChunkEmbedding)) == 0
    IndexService(database).request(admin, kb, 'new-index', 'req_fixture', Provider())
    lease = queue.claim('deleted', ('index',))
    DeletionService(database).delete(admin, document, 'req_delete')
    assert IndexHandler(database, lambda cancel: Provider()).run(queue, lease) == 'cancelled'
    with database.transaction() as session:
        assert session.get(KnowledgeBase, kb).active_index_generation_id is None


def test_rollback_restores_complete_prior_generation(database, tmp_path):
    service, (admin, _, kb) = fixture(database, tmp_path)
    document, revision = upload(service, admin, kb)
    queue = JobRepository(database)
    finish_parse(database, queue)
    first = index(database)
    service.upload(admin, kb, 'deploy.md', b'# Deploy\nPort 9000.', 'replace', 'req_fixture')
    finish_parse(database, queue)
    second = index(database)
    assert second != first
    IndexService(database).rollback(admin, kb, first, 'req_rollback')
    with database.transaction() as session:
        assert session.get(KnowledgeBase, kb).active_index_generation_id == first
        assert session.get(LogicalDocument, document).desired_revision_id == revision
        assert session.get(IndexGeneration, second).status == 'superseded'
        assert candidates(session, kb, first)[0]['text'] == '# Deploy\nPort 8088.'
