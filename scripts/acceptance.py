"""Validate an FA-01..FA-21 evidence manifest and produce the release decision."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.operations.acceptance import validate_acceptance


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    args = parser.parse_args()
    if args.report.exists():
        parser.error('Report already exists; select a new path')
    try:
        report = validate_acceptance(args.manifest, ROOT, ROOT / 'docs/traceability.csv')
    except Exception as exc:
        report = {'status': 'failed', 'error_code': 'ACCEPTANCE_EVIDENCE_INVALID', 'reason': str(exc)}
    report['checked_at'] = datetime.now(timezone.utc).isoformat()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
