"""Preview or apply the documented default retention policy on an explicit database."""
import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db.session import Database
from app.operations.retention import retain
from app.services.errors import DomainError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-env', type=Path)
    parser.add_argument('--expect-database', required=True)
    parser.add_argument('--apply', action='store_true', help='Actually purge expired data; default only counts candidates')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error('Select a new report path')
    values = dotenv_values(args.database_env, encoding='utf-8-sig', interpolate=False) if args.database_env else {}
    url = os.environ.get('DATABASE_URL') or values.get('DATABASE_URL')
    if not url:
        parser.error('DATABASE_URL is required')
    database = Database(url)
    try:
        if database.engine.url.database != args.expect_database:
            raise DomainError('DATABASE_NAME_MISMATCH', 400)
        report = retain(database, apply=args.apply)
    except DomainError as exc:
        report = {'status': 'failed', 'error_code': exc.code}
    except Exception:
        report = {'status': 'failed', 'error_code': 'RETENTION_FAILED'}
    finally:
        database.close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
