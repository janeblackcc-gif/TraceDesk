"""Render and validate the production Compose topology without starting services."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.operations.deployment import config_sha256, validate_compose, validate_nginx

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ENVIRONMENT = {
    'TRACEDESK_APP_IMAGE': 'registry.example.invalid/tracedesk:template@sha256:' + '1' * 64,
    'TRACEDESK_PARSE_WORKER_IMAGE': 'registry.example.invalid/tracedesk-worker:template@sha256:' + '2' * 64,
    'TRACEDESK_PARSER_IMAGE': 'registry.example.invalid/tracedesk-parser:template@sha256:' + '3' * 64,
    'TRACEDESK_PUBLIC_ORIGIN': 'https://tracedesk.example.invalid',
    'TRACEDESK_EMBED_MODEL_DIGEST': '4' * 64,
    'TRACEDESK_CHAT_MODEL_DIGEST': '5' * 64,
    'DOCKER_SOCKET_GID': '999',
}
INTERPOLATION_KEYS = set(TEMPLATE_ENVIRONMENT) | {
    'TRACEDESK_PROXY_IMAGE', 'TRACEDESK_DATA_DIR', 'TRACEDESK_SECRET_DIR', 'TRACEDESK_VLLM_CHAT_URL',
    'TRACEDESK_VLLM_EMBED_URL', 'TRACEDESK_EMBED_MODEL', 'TRACEDESK_CHAT_MODEL',
    'TRACEDESK_EMBED_MODEL_DIGEST', 'TRACEDESK_CHAT_MODEL_DIGEST',
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose-file', type=Path, default=ROOT / 'deploy/compose.yaml')
    parser.add_argument('--nginx-file', type=Path, default=ROOT / 'deploy/nginx.conf')
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--template', action='store_true', help='Validate the example structure with bounded safe substitutions')
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        parser.error('Report already exists; select a new path')
    report: dict[str, object] = {'status': 'running', 'scope': 'template' if args.template else 'production',
                                'checked_at': datetime.now(timezone.utc).isoformat()}
    try:
        if not args.compose_file.is_file() or not args.nginx_file.is_file() or not args.env_file.is_file():
            raise ValueError('Compose, nginx, or environment file is missing')
        environment = {key: value for key, value in os.environ.items() if key not in INTERPOLATION_KEYS}
        if args.template:
            environment.update(TEMPLATE_ENVIRONMENT)
        elif 'REPLACE' in args.env_file.read_text(encoding='utf-8-sig'):
            raise ValueError('Production environment still contains REPLACE placeholders')
        result = subprocess.run(['docker', 'compose', '--env-file', str(args.env_file.resolve()), '--file',
            str(args.compose_file.resolve()), 'config', '--format', 'json'], cwd=ROOT, env=environment,
            capture_output=True, timeout=30, check=False)
        if result.returncode:
            raise RuntimeError('Docker Compose could not render the configuration')
        config = json.loads(result.stdout.decode('utf-8'))
        nginx = args.nginx_file.read_text(encoding='utf-8-sig')
        errors = validate_compose(config, template=args.template) + validate_nginx(nginx)
        report.update(status='failed' if errors else 'passed', errors=errors,
                      services=sorted(config.get('services', {})), config_sha256=config_sha256(config),
                      nginx_sha256=hashlib.sha256(nginx.encode()).hexdigest(),
                      images={name: service.get('image') for name, service in sorted(config.get('services', {}).items())})
    except (OSError, UnicodeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        report.update(status='failed', error_code='DEPLOYMENT_CONFIG_INVALID', reason=str(exc))
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
