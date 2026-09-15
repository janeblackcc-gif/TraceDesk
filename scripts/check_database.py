"""Check database/schema readiness without revealing connection details."""
import json
import sys
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.db.session import Database


def main() -> int:
    url = Settings.load().database_url
    if url is None:
        print(json.dumps({'ready': False, 'code': 'DATABASE_URL_REQUIRED'}))
        return 1
    database = Database(url)
    try:
        status = database.readiness()
        print(json.dumps(asdict(status)))
        return 0 if status.ready else 1
    finally:
        database.close()


if __name__ == '__main__':
    raise SystemExit(main())
