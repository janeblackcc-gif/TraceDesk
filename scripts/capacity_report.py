"""Validate target-host capacity samples and write a bounded summary."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.operations.capacity import evaluate_capacity


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('run', type=Path)
    parser.add_argument('--formal', action='store_true', help='Enforce the 30-minute target capacity contract')
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if args.report.exists():
        parser.error('Report already exists; select a new path')
    try:
        report = evaluate_capacity(args.run, formal=args.formal)
    except Exception as exc:
        report = {'status': 'failed', 'formal': args.formal, 'error_code': 'CAPACITY_EVIDENCE_INVALID',
                  'reason': str(exc)}
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
