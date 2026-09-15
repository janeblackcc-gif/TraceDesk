import os
import tempfile
from pathlib import Path

from sqlalchemy import select

from app.db.models import Job
from app.db.session import Database, DatabaseStatus


def readiness(database: Database, objects: Path) -> DatabaseStatus:
    status = database.readiness()
    if not status.ready:
        return status
    try:
        # A service-owned scratch file, distinct from any original/user content.
        with tempfile.TemporaryFile(dir=objects) as stream:
            stream.write(b'tracedesk-health')
            stream.flush()
            os.fsync(stream.fileno())
            stream.seek(0)
            if stream.read() != b'tracedesk-health':
                return DatabaseStatus(False, 'STORAGE_UNAVAILABLE')
    except OSError:
        return DatabaseStatus(False, 'STORAGE_UNAVAILABLE')
    try:
        with database.transaction() as session:
            session.scalar(select(Job.id).limit(1))
    except Exception:
        return DatabaseStatus(False, 'JOB_REPOSITORY_UNAVAILABLE')
    # The current deployment policy permits evidence-only queries when the local
    # model provider is unavailable. Model-dependent jobs report explicit failure/partial states.
    return DatabaseStatus(True, 'READY')
