from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import signal
import threading

import pytest
from sqlalchemy import func, select

from app.db.models import Job, JobAttempt, KnowledgeBase, User, Workspace
from app.jobs.repository import JobRepository, enqueue
from app.jobs.worker import WorkerShutdown, install_shutdown_handlers, wait_for_notification
from app.services.errors import DomainError


def create_job(database, key='fixture', kind='parse', user_id=None):
    with database.transaction() as session:
        user = session.scalar(select(User))
        if user is None:
            user = User(email='worker@example.invalid', display_name='Worker fixture')
            workspace = Workspace(name='Fixture', slug='fixture')
            session.add_all([user, workspace])
            session.flush()
            kb = KnowledgeBase(workspace_id=workspace.id, name='KB', slug='fixture')
            session.add(kb)
            session.flush()
        else:
            kb = session.scalar(select(KnowledgeBase))
        job = enqueue(session, kind=kind, workspace_id=kb.workspace_id, kb_id=kb.id,
                      created_by=user_id or user.id, key=key, payload={'fixture': key})
        return job.id


def test_worker_sigterm_requests_one_cooperative_shutdown(monkeypatch):
    handlers = {}
    monkeypatch.setattr(signal, 'signal', lambda number, handler: handlers.setdefault(number, handler))
    shutdown = threading.Event()

    install_shutdown_handlers(shutdown)

    with pytest.raises(WorkerShutdown):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert shutdown.is_set()
    handlers[signal.SIGTERM](signal.SIGTERM, None)


def test_shutdown_event_skips_idle_notification_wait():
    class Notifications:
        called = False

        def notifies(self, **_kwargs):
            self.called = True
            return iter(())

    notifications = Notifications()
    shutdown = threading.Event()
    shutdown.set()

    wait_for_notification(notifications, shutdown)

    assert notifications.called is False


def test_two_workers_cannot_claim_same_job(database):
    job_id = create_job(database)
    queue = JobRepository(database)
    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(lambda owner: queue.claim(owner, ('parse',)), ['one', 'two']))
    claimed = [lease for lease in leases if lease]
    assert len(claimed) == 1 and claimed[0].job_id == job_id


def test_idempotency_and_conflicting_payload(database):
    first = create_job(database)
    assert create_job(database) == first
    with database.transaction() as session:
        original = session.get(Job, first)
        with pytest.raises(DomainError, match='IDEMPOTENCY_CONFLICT'):
            enqueue(session, kind='parse', workspace_id=original.workspace_id, kb_id=original.kb_id,
                    created_by=original.created_by, key=original.idempotency_key, payload={'changed': True})


def test_lease_recovery_rejects_old_attempt_even_with_same_owner(database):
    job_id = create_job(database)
    queue = JobRepository(database)
    old = queue.claim('same-worker', ('parse',))
    with database.transaction() as session:
        session.get(Job, job_id).lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert queue.recover() == 1
    current = queue.claim('same-worker', ('parse',))
    assert current.attempt == old.attempt + 1
    assert queue.finish(old, lambda session, job: {'wrong': True}) == 'lease_lost'
    assert queue.finish(current, lambda session, job: {'correct': True}) == 'succeeded'
    with database.transaction() as session:
        assert session.get(Job, job_id).result == {'correct': True}
        assert session.scalar(select(func.count()).select_from(JobAttempt)) == 2


def test_cancel_and_complete_have_one_terminal_state(database):
    job_id = create_job(database)
    queue = JobRepository(database)
    lease = queue.claim('worker', ('parse',))
    with ThreadPoolExecutor(max_workers=2) as executor:
        finished = executor.submit(queue.finish, lease, lambda session, job: {'applied': True})
        cancelled = executor.submit(queue.cancel, job_id)
        finished.result()
        cancelled.result()
    with database.transaction() as session:
        job = session.get(Job, job_id)
        assert job.state in {'succeeded', 'cancelled'}
        assert (job.result == {'applied': True}) == (job.state == 'succeeded')


def test_cancelled_running_job_cannot_commit(database):
    job_id = create_job(database)
    queue = JobRepository(database)
    lease = queue.claim('worker', ('parse',))
    queue.cancel(job_id)
    assert queue.heartbeat(lease) is False
    assert queue.finish(lease, lambda session, job: pytest.fail('Cancelled effect ran')) == 'cancelled'


def test_retry_budget_and_non_retryable_failure(database):
    job_id = create_job(database)
    queue = JobRepository(database)
    for attempt in range(1, 4):
        lease = queue.claim('worker', ('parse',))
        state = queue.fail(lease, 'MODEL_OFFLINE', retryable=True)
        if attempt < 3:
            assert state == 'retry_wait'
            with database.transaction() as session:
                session.get(Job, job_id).available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            queue.recover()
        else:
            assert state == 'failed'
    assert queue.claim('worker', ('parse',)) is None


def test_user_query_backpressure_and_cancel_releases_quota(database):
    first = create_job(database, 'first', 'query')
    create_job(database, 'second', 'query')
    with pytest.raises(DomainError, match='QUEUE_FULL'):
        create_job(database, 'third', 'query')
    JobRepository(database).cancel(first)
    create_job(database, 'third', 'query')


def test_effect_failure_rolls_back_job_and_side_effect(database):
    job_id = create_job(database)
    queue = JobRepository(database)
    lease = queue.claim('worker', ('parse',))
    def effect(session, job):
        session.get(KnowledgeBase, job.kb_id).name = 'Should roll back'
        raise RuntimeError('fixture failure')
    with pytest.raises(RuntimeError, match='fixture failure'):
        queue.finish(lease, effect)
    with database.transaction() as session:
        assert session.get(Job, job_id).state == 'running'
        assert session.scalar(select(KnowledgeBase.name)) == 'KB'
