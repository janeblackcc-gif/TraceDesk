"""Parse worker process. Run with --once for a bounded supervised invocation."""
from __future__ import annotations

import argparse
import signal
import threading
import time
from collections.abc import Iterator
from types import FrameType
from typing import Protocol
from uuid import uuid4

import psycopg

from app.config import Settings
from app.db.session import Database
from app.parsing.worker import DockerParser
from app.storage.object_store import ObjectStore
from .parse_handler import ParseHandler
from .repository import JobRepository
from .gc import GarbageCollector
from .index_handler import IndexHandler
from .query_handler import QueryHandler
from app.models.factory import create_bounded_provider
from app.services.errors import DomainError
from app.observability import logging as operational_log
from app.observability.tracing import provider as telemetry_provider


class WorkerShutdown(BaseException):
    """Unwind an in-flight job so transactional and subprocess cleanup runs."""


class NotificationSource(Protocol):
    def notifies(self, *, timeout: float, stop_after: int) -> Iterator[object]: ...


def install_shutdown_handlers(shutdown: threading.Event) -> None:
    """Make SIGTERM/SIGINT effective when Python is the container's PID 1."""
    def request_shutdown(_signum: int, _frame: FrameType | None) -> None:
        if shutdown.is_set():
            return
        shutdown.set()
        raise WorkerShutdown

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)


def wait_for_notification(notifications: NotificationSource, shutdown: threading.Event) -> None:
    """Wait briefly for PostgreSQL work while retaining a signal-event fallback."""
    if shutdown.is_set():
        return
    for _ in notifications.notifies(timeout=1, stop_after=1):
        break


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--parser-image', default='tracedesk-parser:industrial-20260909')
    parser.add_argument('--kind', choices=('parse', 'gc', 'index', 'query'), default='parse')
    args = parser.parse_args()
    settings = Settings.load()
    if settings.database_url is None:
        parser.error('DATABASE_URL is required')
    database = Database(settings.database_url)
    operational_log.configure()
    telemetry = telemetry_provider('tracedesk-' + args.kind)
    tracer = telemetry.get_tracer('tracedesk.jobs')
    queue = JobRepository(database)
    handler = ParseHandler(database, ObjectStore(settings.data_dir / 'objects'), DockerParser(args.parser_image))
    collector = GarbageCollector(database, handler.objects)
    indexer = IndexHandler(database, lambda cancel: create_bounded_provider(settings, cancel=cancel))
    query_handler = QueryHandler(database, lambda cancel: create_bounded_provider(settings, cancel=cancel))
    owner = args.kind + '-' + uuid4().hex
    shutdown = threading.Event()
    install_shutdown_handlers(shutdown)
    try:
        if not database.readiness().ready:
            raise RuntimeError('DATABASE_NOT_READY')
        # LISTEN wakes idle workers without periodic process/shell polling.
        url = database.engine.url
        with psycopg.connect(host=url.host, port=url.port or 5432, user=url.username,
                password=url.password, dbname=url.database, autocommit=True, connect_timeout=5) as notifications:
            notifications.execute('LISTEN tracedesk_jobs')
            while not shutdown.is_set():
                if args.kind == 'parse':
                    handler.parser.reap_expired()
                try:
                    queue.recover()
                    lease = queue.claim(owner, ('index', 'reindex') if args.kind == 'index' else (args.kind,))
                except DomainError as exc:
                    if exc.code != 'MAINTENANCE_IN_PROGRESS':
                        raise
                    if args.once:
                        print('MAINTENANCE_IN_PROGRESS', flush=True)
                        return 75
                    wait_for_notification(notifications, shutdown)
                    continue
                if lease is not None:
                    started = time.perf_counter()
                    with tracer.start_as_current_span('job.' + args.kind, record_exception=False,
                                                      set_status_on_exception=False) as span:
                        try:
                            selected = {'parse': handler, 'gc': collector, 'index': indexer, 'query': query_handler}[args.kind]
                            state = selected.run(queue, lease)
                        except Exception:
                            state = queue.fail(lease, 'WORKER_EXECUTION_FAILED')
                        operational_log.event('job.' + state, service=args.kind, job_id=lease.job_id,
                            latency_ms=(time.perf_counter() - started) * 1000,
                            trace_id=f'{span.get_span_context().trace_id:032x}')
                    if args.once:
                        return 0 if state in {'succeeded', 'cancelled', 'superseded'} else 1
                elif args.once:
                    print('No eligible job', flush=True)
                    return 0
                else:
                    wait_for_notification(notifications, shutdown)
    except WorkerShutdown:
        return 0
    finally:
        telemetry.shutdown()
        database.close()


if __name__ == '__main__':
    raise SystemExit(main())
