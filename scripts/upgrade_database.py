"""Preflight or apply a schema upgrade using a verified backup rollback boundary."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db.session import Database
from app.operations.upgrade import upgrade
from app.services.errors import DomainError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path, help='Completed backup directory with manifest.json')
    parser.add_argument('--database-env', type=Path, help='Restricted UTF-8 file containing DATABASE_URL')
    parser.add_argument('--expect-database', required=True, help='Exact target database name')
    parser.add_argument('--apply', action='store_true', help='Apply migrations; default is a read-only plan')
    parser.add_argument('--trusted-backup', action='store_true', help='Confirm the rollback backup is operator-controlled')
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if args.report.exists():
        parser.error('Report already exists; select a new path')
    if args.apply and not args.trusted_backup:
        parser.error('--trusted-backup is required with --apply')
    values = dotenv_values(args.database_env, encoding='utf-8-sig', interpolate=False) if args.database_env else {}
    value = os.environ.get('DATABASE_URL') or values.get('DATABASE_URL')
    if not value:
        parser.error('DATABASE_URL is required')
    report: dict[str, object] = {
        'status': 'running',
        'mode': 'apply' if args.apply else 'plan',
        'started_at': datetime.now(timezone.utc).isoformat(),
    }
    database = Database(value)
    try:
        if database.engine.url.database != args.expect_database:
            raise DomainError('DATABASE_NAME_MISMATCH', 400)
        report.update(upgrade(database, args.archive, apply=args.apply))
    except DomainError as exc:
        report.update(status='failed', error_code=exc.code)
    except Exception:
        report.update(status='failed', error_code='MIGRATION_FAILED' if args.apply else 'UPGRADE_PREFLIGHT_FAILED')
    finally:
        database.close()
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report))
    return 0 if report['status'] in {'planned', 'passed'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
