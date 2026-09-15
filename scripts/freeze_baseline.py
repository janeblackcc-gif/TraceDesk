"""Freeze immutable Git inputs and the approved design without private data."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = 'ccc00c2f5e369bb50a7e459fda0404de1e74b4f3'
SPEC = ROOT / 'docs/industrial/specification/design-baseline-1.0'


def git(*args: str) -> bytes:
    return subprocess.check_output(['git', '-C', str(ROOT), *args])


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def snapshot() -> dict:
    inputs = []
    for name in git('ls-tree', '-r', '--name-only', BASELINE).decode().splitlines():
        if name.startswith(('app/', 'tests/', 'datasets/', 'eval/', 'web/', 'docs/', '.github/')) or name in {
                'requirements.txt', 'requirements-dev.txt', 'README.md'}:
            inputs.append({'path': name, 'sha256': digest(git('show', f'{BASELINE}:{name}'))})
    specification = []
    for line in (SPEC / 'MANIFEST.sha256').read_text(encoding='utf-8-sig').splitlines():
        match = re.fullmatch(r'([a-f0-9]{64})\s+\*?(.+)', line)
        if not match:
            raise ValueError('Invalid specification manifest')
        expected, name = match.groups()
        path = (SPEC / name).resolve()
        if not path.is_relative_to(SPEC.resolve()) or digest(path.read_bytes()) != expected:
            raise ValueError(f'Specification hash mismatch: {name}')
        specification.append({'path': name, 'sha256': expected})
    models = []
    for item in inputs:
        if item['path'].startswith('eval/baselines/') and item['path'].endswith('/manifest.json'):
            models.append({'path': item['path'], 'manifest': json.loads(git('show', f"{BASELINE}:{item['path']}"))})
    return {'commit': BASELINE, 'classification': 'legacy_dev', 'inputs': inputs,
            'specification': specification, 'model_evidence': models,
            'rerun': {'pytest': 179, 'model_inference': False, 'browser_acceptance': False}}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--verify', action='store_true')
    parser.add_argument('--output', type=Path, default=ROOT / 'artifacts/baseline/manifest.json')
    args = parser.parse_args()
    current = snapshot()
    if args.verify:
        saved = json.loads(args.output.read_text(encoding='utf-8'))
        saved.pop('created_at')
        if saved != current:
            raise SystemExit('Baseline verification failed')
        print(f"Verified {len(current['inputs'])} Git inputs and design hashes")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8') as stream:
            json.dump({'created_at': datetime.now(timezone.utc).isoformat(), **current}, stream, ensure_ascii=False, indent=2)
        print(f"Frozen {len(current['inputs'])} Git inputs")


if __name__ == '__main__':
    main()
