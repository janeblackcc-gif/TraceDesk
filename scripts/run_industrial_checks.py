"""Run all available checks with PostgreSQL required and evidence preserved."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-env', type=Path, help='Private dotenv containing TRACEDESK_TEST_DATABASE_URL')
    parser.add_argument('--output', required=True, type=Path, help='New evidence directory')
    parser.add_argument('--parser-image', help='Built isolated parser image; required for complete verification')
    parser.add_argument('--postgres-container', help='PG18 container for real pg_dump/pg_restore checks; otherwise native PG18 tools')
    args = parser.parse_args()
    environment = dict(os.environ, PYTHONUTF8='1')
    if args.database_env:
        values = dotenv_values(args.database_env, encoding='utf-8-sig', interpolate=False)
        value = values.get('TRACEDESK_TEST_DATABASE_URL')
        if value:
            environment['TRACEDESK_TEST_DATABASE_URL'] = value
    if not environment.get('TRACEDESK_TEST_DATABASE_URL'):
        parser.error('PostgreSQL tests are required; set TRACEDESK_TEST_DATABASE_URL')
    if args.parser_image:
        environment['TRACEDESK_PARSER_TEST_IMAGE'] = args.parser_image
    if args.postgres_container:
        environment['TRACEDESK_BACKUP_TEST_CONTAINER'] = args.postgres_container
    if not environment.get('TRACEDESK_PARSER_TEST_IMAGE'):
        parser.error('Container parser tests are required; set --parser-image or TRACEDESK_PARSER_TEST_IMAGE')
    # The test fixture creates disposable databases; the base URL only supplies
    # a connection with CREATE DATABASE privileges. Do not activate team mode
    # globally when running the preserved RC2 tests.
    environment.pop('DATABASE_URL', None)
    args.output.mkdir(parents=True, exist_ok=False)
    environment['TRACEDESK_TEST_ARTIFACT_DIR'] = str(args.output.resolve())
    report = {'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat(), 'checks': []}
    steps = [
        ('dependencies', ['-m', 'pip', 'check']),
        ('locks', ['scripts/check_locks.py']),
        ('audit', ['-m', 'pip_audit', '--requirement', 'requirements-dev-lock.txt', '--require-hashes', '--disable-pip',
                   '--progress-spinner', 'off', '--format', 'json', '--output', str((args.output / 'audit.json').resolve())]),
        ('types', ['-m', 'mypy']),
        ('lint', ['-m', 'ruff', 'check', 'app/api', 'app/application.py', 'app/audit', 'app/auth', 'app/authz', 'app/db',
                  'app/jobs', 'app/migration', 'app/models', 'app/operations', 'app/observability', 'app/parsing',
                  'app/rag', 'app/services', 'app/storage', 'tests/industrial']),
        ('openapi', ['scripts/check_openapi.py']),
        ('eval-schema', ['scripts/check_eval_dataset.py', '--check-schema', 'eval/schema_v2.json']),
        ('deployment', ['scripts/check_deployment.py', '--env-file', '.env.production.example', '--template',
                        '--report', str((args.output / 'deployment.json').resolve())]),
        ('traceability', ['scripts/check_traceability.py']),
        ('pytest', ['-m', 'pytest', '-q', '--junitxml=' + str((args.output / 'pytest.xml').resolve())]),
        ('release', ['scripts/release_check.py', '--output', str((args.output / 'release.json').resolve())]),
    ]
    for name, command in steps:
        print(f'Running {name}', flush=True)
        with (args.output / f'{name}.log').open('x', encoding='utf-8') as log:
            try:
                result = subprocess.run([sys.executable, *command], cwd=ROOT, env=environment,
                                        stdout=log, stderr=subprocess.STDOUT, timeout=600, check=False)
                exit_code = result.returncode
                if name == 'pytest' and exit_code == 0:
                    suites = ET.parse(args.output / 'pytest.xml').getroot().iter('testsuite')
                    if any(int(suite.get('skipped', '0')) for suite in suites):
                        exit_code = 1
                        log.write('\nRequired verification cannot pass with skipped tests.\n')
            except subprocess.TimeoutExpired:
                exit_code = 124
        report['checks'].append({'name': name, 'exit_code': exit_code, 'finished_at': datetime.now(timezone.utc).isoformat()})
        if exit_code != 0:
            report['status'] = 'failed'
        (args.output / 'status.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        if exit_code != 0:
            print(f'{name} failed; original evidence retained')
            return 1
    report['status'] = 'passed'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    (args.output / 'status.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('All implemented checks passed; quality and pilot gates are separate.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
