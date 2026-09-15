"""Inspect a frozen RC2 database or import it with an existing administrator."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import Settings
from app.db.session import Database
from app.migration.importer import import_snapshot
from app.migration.legacy import LegacyValidationError, file_hash, inspect_legacy
from app.storage.object_store import ObjectStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path, help='New private evidence directory')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--dry-run', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--admin-id', type=UUID)
    parser.add_argument('--objects-dir', type=Path)
    parser.add_argument('--max-documents', type=int, help='Bound each checkpointed import invocation')
    args = parser.parse_args()
    if args.apply and args.admin_id is None:
        parser.error('--apply requires the existing operator --admin-id')
    args.output.mkdir(parents=True, exist_ok=False)
    try:
        snapshot = inspect_legacy(args.source)
    except LegacyValidationError as exc:
        (args.output / 'migration_plan.json').write_text(json.dumps(exc.report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('LEGACY_VALIDATION_FAILED')
        return 1
    (args.output / 'migration_plan.json').write_text(json.dumps(snapshot.report, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.dry_run:
        print(json.dumps({'status': 'passed', 'documents': len(snapshot.documents), 'source_unchanged': True}))
        return 0
    settings = Settings.load()
    if settings.database_url is None:
        parser.error('DATABASE_URL is required for --apply')
    if file_hash(args.source) != snapshot.source_hash:
        raise RuntimeError('SOURCE_CHANGED_BEFORE_IMPORT')
    database = Database(settings.database_url)
    try:
        result = import_snapshot(database, ObjectStore(args.objects_dir or settings.data_dir / 'objects'), snapshot,
                                 args.admin_id, max_documents=args.max_documents)
        report = asdict(result)
        report['source_unchanged'] = file_hash(args.source) == snapshot.source_hash
        (args.output / 'migration_result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps({'status': 'passed' if report['source_unchanged'] else 'failed',
                          'imported': result.imported, 'already_present': result.already_present, 'remaining': result.remaining}))
        return 0 if report['source_unchanged'] and result.remaining == 0 else 2
    finally:
        database.close()


if __name__ == '__main__':
    raise SystemExit(main())
