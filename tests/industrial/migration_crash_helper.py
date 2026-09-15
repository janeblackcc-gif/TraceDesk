"""Child-process fault injection using synthetic test data only."""
import os
import sys
import threading
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.db.session import Database
from app.migration.importer import import_snapshot
from app.migration.legacy import inspect_legacy
from app.storage.object_store import ObjectStore


class CrashPointStore(ObjectStore):
    count = 0

    def put(self, *args, **kwargs):
        result = super().put(*args, **kwargs)
        self.count += 1
        if self.count == 2:
            print('CRASH_POINT_AFTER_OBJECT', flush=True)
            threading.Event().wait()
        return result


database = Database(os.environ['MIGRATION_TEST_URL'])
try:
    import_snapshot(database, CrashPointStore(Path(sys.argv[2])), inspect_legacy(Path(sys.argv[1])), UUID(sys.argv[3]))
finally:
    database.close()
