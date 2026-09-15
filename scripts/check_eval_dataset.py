"""Validate a private evaluation dataset without copying questions or gold into the report."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.operations.eval_dataset import schema_bundle, validate_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('dataset', nargs='?', type=Path)
    parser.add_argument('--formal', action='store_true', help='Require at least 40 double-reviewed cases and sealed holdout')
    parser.add_argument('--report', type=Path)
    schemas = parser.add_mutually_exclusive_group()
    schemas.add_argument('--schema-output', type=Path, help='Write the public v2 JSON schema and exit')
    schemas.add_argument('--check-schema', type=Path, help='Fail if a committed schema differs from the code model')
    args = parser.parse_args()
    rendered_schema = json.dumps(schema_bundle(), ensure_ascii=False, indent=2) + '\n'
    if args.schema_output:
        if args.schema_output.exists():
            parser.error('Schema output already exists')
        args.schema_output.parent.mkdir(parents=True, exist_ok=True)
        args.schema_output.write_text(rendered_schema, encoding='utf-8')
        return 0
    if args.check_schema:
        if not args.check_schema.is_file():
            print('Evaluation schema file is missing')
            return 1
        try:
            committed_schema = json.loads(args.check_schema.read_text(encoding='utf-8-sig'))
        except (OSError, json.JSONDecodeError):
            print('Evaluation schema is not valid UTF-8 JSON')
            return 1
        if committed_schema != schema_bundle():
            print('Evaluation schema differs from the reviewed code model')
            return 1
        print('Evaluation schema matches the reviewed code model')
        return 0
    if args.dataset is None or args.report is None:
        parser.error('dataset and --report are required')
    if args.report.exists():
        parser.error('Report already exists; select a new path')
    try:
        report = validate_dataset(args.dataset, formal=args.formal)
    except Exception as exc:
        report = {'status': 'failed', 'formal': args.formal, 'error_code': 'EVAL_DATASET_INVALID',
                  'reason': str(exc)}
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
