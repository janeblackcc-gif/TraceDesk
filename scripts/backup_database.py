"""Operator backup/restore for team mode; restore never overwrites an existing target."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db.session import Database
from app.operations.backup import PostgresTools, backup, restore, verify_backup
from app.services.errors import DomainError
from app.storage.object_store import ObjectStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('backup', 'verify', 'restore'))
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--database-env', type=Path, help='Restricted UTF-8 file containing DATABASE_URL')
    parser.add_argument('--expect-database', help='Required exact database name to guard connection selection')
    parser.add_argument('--objects', type=Path)
    parser.add_argument('--postgres-container', help='Explicit PG18 container; omit for native PG18 tools')
    parser.add_argument('--app-ref', help='Pinned app image digest, or dev:<source hash> for local drills')
    parser.add_argument('--trusted-backup', action='store_true', help='Restore only operator-trusted archives (dumps execute SQL)')
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if args.report.exists():
        parser.error('Report already exists; select a new path')
    database = None
    report = {'operation': args.operation, 'status': 'running'}
    try:
        if args.operation == 'verify':
            manifest = verify_backup(args.archive)
        else:
            if not args.expect_database or not args.objects:
                parser.error('--expect-database and --objects are required')
            if args.operation == 'restore' and not args.trusted_backup:
                parser.error('--trusted-backup is required; restore executes SQL from the archive')
            if args.operation == 'backup' and not args.app_ref:
                parser.error('--app-ref is required')
            config = dotenv_values(args.database_env, encoding='utf-8-sig', interpolate=False) if args.database_env else {}
            value = os.environ.get('DATABASE_URL') or config.get('DATABASE_URL')
            if not value:
                parser.error('DATABASE_URL is required in the environment or --database-env')
            database = Database(value)
            if database.engine.url.database != args.expect_database:
                raise DomainError('DATABASE_NAME_MISMATCH', 400)
            tools = PostgresTools(database.engine.url, container=args.postgres_container)
            if args.operation == 'backup':
                if not args.objects.is_dir():
                    raise DomainError('OBJECT_ROOT_MISSING', 400)
                manifest = backup(database, ObjectStore(args.objects), args.archive, tools, app_ref=args.app_ref)
            else:
                manifest = restore(database, args.objects, args.archive, tools)
        report.update(status='passed', schema_revision=manifest.schema_revision,
                      object_count=len(manifest.objects), table_count=len(manifest.tables))
    except DomainError as exc:
        report.update(status='failed', error_code=exc.code)
    except Exception:
        report.update(status='failed', error_code='BACKUP_OPERATION_FAILED')
    finally:
        if database:
            database.close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
