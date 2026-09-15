from __future__ import annotations

import threading
from contextlib import contextmanager
from collections.abc import Iterator

from .repository import JobRepository, Lease


@contextmanager
def lease_heartbeat(queue: JobRepository, lease: Lease) -> Iterator[threading.Event]:
    cancelled, finished = threading.Event(), threading.Event()
    def renew() -> None:
        while not finished.wait(min(10, queue.lease_seconds / 3)):
            try:
                if queue.heartbeat(lease):
                    continue
            except Exception:
                pass
            cancelled.set()
            return
    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        yield cancelled
    finally:
        finished.set()
        thread.join(timeout=15)
