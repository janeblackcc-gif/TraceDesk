"""Static validation for the reviewed single-host Compose topology."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlparse

EXPECTED_SERVICES = {'postgres', 'migrate', 'web', 'parse-worker', 'index-worker', 'query-worker', 'gc-worker', 'proxy'}
RUNTIME_SERVICES = {'migrate', 'web', 'parse-worker', 'index-worker', 'query-worker', 'gc-worker'}
WORKERS = {'parse-worker', 'index-worker', 'query-worker', 'gc-worker'}
PINNED_IMAGE = re.compile(r'^\S+@sha256:[0-9a-f]{64}$')
MODEL_DIGEST = re.compile(r'^[0-9a-f]{64}$')
NGINX_TEMP_PATHS = {
    'client_body_temp_path': '/tmp/client_body',
    'proxy_temp_path': '/tmp/proxy',
    'fastcgi_temp_path': '/tmp/fastcgi',
    'uwsgi_temp_path': '/tmp/uwsgi',
    'scgi_temp_path': '/tmp/scgi',
}


def _secret_names(service: dict) -> set[str]:
    return {item['source'] if isinstance(item, dict) else item for item in service.get('secrets', [])}


def _volume_targets(service: dict) -> dict[str, dict]:
    return {item.get('target'): item for item in service.get('volumes', []) if isinstance(item, dict)}


def _local_model_endpoint(value: object) -> bool:
    try:
        parsed = urlparse(str(value))
        port = parsed.port
    except ValueError:
        return False
    return (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'} and
            (port is None or 1 <= port <= 65535) and not parsed.path and not parsed.query and
            not parsed.fragment and parsed.username is None and parsed.password is None)


def _duration_seconds(value: object) -> int | None:
    match = re.fullmatch(r'(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?', str(value))
    if match is None or not any(match.groups()):
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def validate_nginx(config: str) -> list[str]:
    """Validate directives needed by the reviewed read-only proxy container."""
    errors = []
    if not re.search(r'(?m)^\s*listen\s+443\s+ssl;\s*$', config):
        errors.append('NGINX_TLS_LISTENER_MISSING')
    for directive, path in NGINX_TEMP_PATHS.items():
        if not re.search(rf'(?m)^\s*{directive}\s+{re.escape(path)};\s*$', config):
            errors.append(f'NGINX_TEMP_PATH_INVALID:{directive}')
    return sorted(errors)


def validate_compose(config: dict, *, template: bool = False) -> list[str]:
    errors = []
    services = config.get('services')
    if not isinstance(services, dict) or set(services) != EXPECTED_SERVICES:
        return ['SERVICE_SET_INVALID']
    for name, service in services.items():
        image = service.get('image', '')
        if not isinstance(image, str) or not PINNED_IMAGE.fullmatch(image):
            errors.append(f'IMAGE_NOT_PINNED:{name}')
        if not template and ('REPLACE' in image or '.invalid/' in image):
            errors.append(f'IMAGE_PLACEHOLDER:{name}')
        if 'build' in service:
            errors.append(f'RUNTIME_BUILD_FORBIDDEN:{name}')
        environment = service.get('environment', {})
        if isinstance(environment, dict) and any(
                ('PASSWORD' in key or 'TOKEN' in key or key == 'DATABASE_URL') and not key.endswith('_FILE')
                for key in environment):
            errors.append(f'DIRECT_SECRET_ENV:{name}')
    for name in RUNTIME_SERVICES:
        service = services[name]
        environment = service.get('environment', {})
        if service.get('network_mode') != 'host':
            errors.append(f'HOST_NETWORK_REQUIRED:{name}')
        if service.get('user') != '65532:65532' or service.get('read_only') is not True:
            errors.append(f'RUNTIME_IDENTITY_INVALID:{name}')
        if 'ALL' not in service.get('cap_drop', []) or 'no-new-privileges:true' not in service.get('security_opt', []):
            errors.append(f'RUNTIME_HARDENING_INVALID:{name}')
        if not any('/tmp:' in item and 'noexec' in item and 'nosuid' in item for item in service.get('tmpfs', [])):
            errors.append(f'RUNTIME_TMPFS_INVALID:{name}')
        if environment.get('TRACEDESK_DEPLOYMENT') != 'production' or environment.get('DATABASE_URL_FILE') != '/run/secrets/database_url':
            errors.append(f'RUNTIME_CONFIG_INVALID:{name}')
        data_dir = environment.get('TRACEDESK_DATA_DIR')
        volumes = _volume_targets(service)
        data_volume = volumes.get(data_dir)
        if (not isinstance(data_dir, str) or not data_dir.startswith('/') or 'REPLACE' in data_dir or
                not data_volume or data_volume.get('type') != 'bind' or data_volume.get('source') != data_dir):
            errors.append(f'DATA_BIND_INVALID:{name}')
        origin = urlparse(str(environment.get('TRACEDESK_PUBLIC_ORIGIN', '')))
        if (origin.scheme != 'https' or not origin.hostname or origin.path or origin.query or origin.fragment or
                (not template and (origin.hostname.endswith('.invalid') or 'replace' in origin.hostname.lower()))):
            errors.append(f'PUBLIC_ORIGIN_INVALID:{name}')
        if environment.get('TRACEDESK_MODEL_PROVIDER') != 'vllm':
            errors.append(f'MODEL_PROVIDER_INVALID:{name}')
        for endpoint in ('TRACEDESK_VLLM_CHAT_URL', 'TRACEDESK_VLLM_EMBED_URL'):
            if not _local_model_endpoint(environment.get(endpoint, '')):
                errors.append(f'VLLM_BOUNDARY_INVALID:{name}:{endpoint}')
        for digest_key in ('TRACEDESK_EMBED_MODEL_DIGEST', 'TRACEDESK_CHAT_MODEL_DIGEST'):
            digest = str(environment.get(digest_key, ''))
            if not MODEL_DIGEST.fullmatch(digest):
                errors.append(f'MODEL_DIGEST_INVALID:{name}:{digest_key}')
        for model_key in ('TRACEDESK_EMBED_MODEL', 'TRACEDESK_CHAT_MODEL'):
            model = str(environment.get(model_key, ''))
            if not model or any(character.isspace() for character in model) or 'cloud' in model.lower():
                errors.append(f'MODEL_NAME_INVALID:{name}:{model_key}')
        if 'database_url' not in _secret_names(service):
            errors.append(f'DATABASE_SECRET_MISSING:{name}')
        if name != 'migrate':
            dependencies = service.get('depends_on', {})
            if (dependencies.get('postgres', {}).get('condition') != 'service_healthy' or
                    dependencies.get('migrate', {}).get('condition') != 'service_completed_successfully'):
                errors.append(f'STARTUP_ORDER_INVALID:{name}')
    if services['migrate'].get('restart') != 'no' or services['migrate'].get('command') != ['-m', 'alembic', 'upgrade', 'head']:
        errors.append('MIGRATION_SERVICE_INVALID')
    if services['migrate'].get('depends_on', {}).get('postgres', {}).get('condition') != 'service_healthy':
        errors.append('MIGRATION_DEPENDENCY_INVALID')
    for name in WORKERS:
        grace = _duration_seconds(services[name].get('stop_grace_period'))
        if grace is None or grace < 120:
            errors.append(f'WORKER_STOP_GRACE_INVALID:{name}')
    socket_users = {name for name, service in services.items()
                    if '/var/run/docker.sock' in _volume_targets(service)}
    if socket_users != {'parse-worker'}:
        errors.append('DOCKER_SOCKET_SCOPE_INVALID')
    parser = services['parse-worker']
    groups = parser.get('group_add', [])
    if len(groups) != 1 or not str(groups[0]).isdigit() or int(groups[0]) <= 0:
        errors.append('DOCKER_SOCKET_GROUP_INVALID')
    command = parser.get('command', [])
    try:
        parser_image = command[command.index('--parser-image') + 1]
    except (ValueError, IndexError):
        parser_image = ''
    if not isinstance(parser_image, str) or not PINNED_IMAGE.fullmatch(parser_image):
        errors.append('PARSER_IMAGE_NOT_PINNED')
    elif not template and ('REPLACE' in parser_image or '.invalid/' in parser_image):
        errors.append('PARSER_IMAGE_PLACEHOLDER')
    web = services['web']
    if 'bootstrap_token' not in _secret_names(web) or web.get('environment', {}).get('TRACEDESK_BOOTSTRAP_TOKEN_FILE') != '/run/secrets/bootstrap_token':
        errors.append('BOOTSTRAP_SECRET_INVALID')
    if any('bootstrap_token' in _secret_names(services[name]) for name in RUNTIME_SERVICES - {'web'}):
        errors.append('BOOTSTRAP_SECRET_SCOPE_INVALID')
    health = web.get('healthcheck', {})
    if '/readyz' not in ' '.join(str(item) for item in health.get('test', [])):
        errors.append('WEB_HEALTHCHECK_INVALID')
    postgres = services['postgres']
    ports = postgres.get('ports', [])
    if (len(ports) != 1 or ports[0].get('host_ip') != '127.0.0.1' or ports[0].get('target') != 5432 or
            'database_password' not in _secret_names(postgres) or
            postgres.get('environment', {}).get('POSTGRES_PASSWORD_FILE') != '/run/secrets/database_password'):
        errors.append('POSTGRES_EXPOSURE_INVALID')
    if not postgres.get('healthcheck'):
        errors.append('POSTGRES_HEALTHCHECK_MISSING')
    if any(service.get('ports') for name, service in services.items() if name != 'postgres'):
        errors.append('UNEXPECTED_PUBLISHED_PORT')
    proxy = services['proxy']
    if (proxy.get('network_mode') != 'host' or proxy.get('user') != '65532:65532' or proxy.get('read_only') is not True or
            'ALL' not in proxy.get('cap_drop', []) or set(proxy.get('cap_add', [])) != {'NET_BIND_SERVICE'} or
            'no-new-privileges:true' not in proxy.get('security_opt', []) or
            not any('/tmp:' in item and 'noexec' in item and 'nosuid' in item for item in proxy.get('tmpfs', [])) or
            not {'tls_certificate', 'tls_private_key'} <= _secret_names(proxy) or
            proxy.get('depends_on', {}).get('web', {}).get('condition') != 'service_healthy'):
        errors.append('PROXY_HARDENING_INVALID')
    nginx = _volume_targets(proxy).get('/etc/nginx/nginx.conf')
    if (not nginx or nginx.get('read_only') is not True or
            not str(nginx.get('source', '')).replace('\\', '/').endswith('/deploy/nginx.conf')):
        errors.append('PROXY_CONFIG_MOUNT_INVALID')
    secret_config = config.get('secrets', {})
    if set(secret_config) != {'database_url', 'database_password', 'bootstrap_token', 'tls_certificate', 'tls_private_key'}:
        errors.append('SECRET_SET_INVALID')
    for name, value in secret_config.items():
        filename = value.get('file', '') if isinstance(value, dict) else ''
        if not isinstance(filename, str) or not filename.startswith('/') or (not template and 'REPLACE' in filename):
            errors.append(f'SECRET_PATH_INVALID:{name}')
    return sorted(set(errors))


def config_sha256(config: dict) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
