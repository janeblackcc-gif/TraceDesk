"""Verify reviewed dependency inputs/locks, or record hashes after recompilation."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ('requirements.txt', 'requirements-dev.txt', 'deploy/quality-requirements.txt', 'deploy/parser-requirements.txt',
         'requirements-lock.txt', 'requirements-dev-lock.txt', 'deploy/parser-requirements-lock.txt')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record', action='store_true', help='Record hashes only after compiling/reviewing all locks')
    args = parser.parse_args()
    current = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}
    path = ROOT / 'deploy/lock-inputs.json'
    if args.record:
        path.write_text(json.dumps(current, indent=2) + '\n', encoding='utf-8')
        print('Dependency lock hashes recorded')
        return 0
    expected = json.loads(path.read_text(encoding='utf-8'))
    changed = sorted(name for name in FILES if expected.get(name) != current[name])
    if changed:
        print(json.dumps({'status': 'failed', 'changed': changed}))
        return 1
    print('Dependency inputs and hashes match reviewed locks')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
