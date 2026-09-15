from copy import deepcopy
from pathlib import Path

from app.operations.deployment import validate_compose, validate_nginx

PIN = '@sha256:' + 'a' * 64


def runtime(kind):
    command = ['-m', 'app.jobs.worker', '--kind', kind]
    if kind == 'parse':
        command += ['--parser-image', 'registry.example/parser:1' + PIN]
    return {
        'image': 'registry.example/app:1' + PIN, 'network_mode': 'host', 'user': '65532:65532',
        'read_only': True, 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
        'tmpfs': ['/tmp:rw,noexec,nosuid,size=134217728'], 'restart': 'unless-stopped', 'command': command,
        'environment': {'DATABASE_URL_FILE': '/run/secrets/database_url', 'TRACEDESK_DEPLOYMENT': 'production',
            'TRACEDESK_DATA_DIR': '/srv/tracedesk/data', 'TRACEDESK_PUBLIC_ORIGIN': 'https://trace.example.com',
            'TRACEDESK_MODEL_PROVIDER': 'vllm',
            'TRACEDESK_VLLM_CHAT_URL': 'http://127.0.0.1:8000',
            'TRACEDESK_VLLM_EMBED_URL': 'http://127.0.0.1:8001',
            'TRACEDESK_EMBED_MODEL': 'Qwen/Qwen3-Embedding-0.6B',
            'TRACEDESK_CHAT_MODEL': 'Qwen/Qwen3-4B-Instruct-2507',
            'TRACEDESK_EMBED_MODEL_DIGEST': 'a' * 64,
            'TRACEDESK_CHAT_MODEL_DIGEST': 'b' * 64},
        'secrets': [{'source': 'database_url'}],
        'volumes': [{'type': 'bind', 'source': '/srv/tracedesk/data', 'target': '/srv/tracedesk/data'}],
        'depends_on': {'postgres': {'condition': 'service_healthy'}, 'migrate': {'condition': 'service_completed_successfully'}},
    }


def valid_config():
    services = {name: runtime(name.removesuffix('-worker')) for name in
                ('parse-worker', 'index-worker', 'query-worker', 'gc-worker')}
    for service in services.values():
        service['stop_grace_period'] = '120s'
    services['migrate'] = runtime('migrate')
    services['migrate'].update(command=['-m', 'alembic', 'upgrade', 'head'], restart='no',
                               depends_on={'postgres': {'condition': 'service_healthy'}})
    services['web'] = runtime('web')
    services['web']['command'] = None
    services['web']['environment']['TRACEDESK_BOOTSTRAP_TOKEN_FILE'] = '/run/secrets/bootstrap_token'
    services['web']['secrets'].append({'source': 'bootstrap_token'})
    services['web']['healthcheck'] = {'test': ['CMD', 'python', '-c', "urlopen('http://127.0.0.1:8765/readyz')"]}
    services['parse-worker']['group_add'] = ['999']
    services['parse-worker']['volumes'].append({'type': 'bind', 'source': '/var/run/docker.sock',
                                                'target': '/var/run/docker.sock'})
    services['postgres'] = {'image': 'registry.example/postgres:18' + PIN, 'environment': {
        'POSTGRES_PASSWORD_FILE': '/run/secrets/database_password'}, 'secrets': [{'source': 'database_password'}],
        'ports': [{'host_ip': '127.0.0.1', 'target': 5432}], 'healthcheck': {'test': ['CMD', 'pg_isready']}}
    services['proxy'] = {'image': 'registry.example/nginx:1' + PIN, 'network_mode': 'host', 'user': '65532:65532',
        'read_only': True, 'cap_drop': ['ALL'], 'cap_add': ['NET_BIND_SERVICE'],
        'security_opt': ['no-new-privileges:true'], 'tmpfs': ['/tmp:rw,noexec,nosuid,size=134217728'],
        'secrets': [{'source': 'tls_certificate'}, {'source': 'tls_private_key'}],
        'depends_on': {'web': {'condition': 'service_healthy'}},
        'volumes': [{'type': 'bind', 'source': '/src/deploy/nginx.conf', 'target': '/etc/nginx/nginx.conf',
                     'read_only': True}]}
    secrets = {name: {'file': '/srv/tracedesk/secrets/' + name} for name in
               ('database_url', 'database_password', 'bootstrap_token', 'tls_certificate', 'tls_private_key')}
    return {'services': services, 'secrets': secrets}


def test_reviewed_compose_contract_and_high_risk_regressions():
    config = valid_config()
    assert validate_compose(config) == []
    broken = deepcopy(config)
    broken['services']['web']['read_only'] = False
    broken['services']['web']['image'] = 'registry.example/app:latest'
    broken['services']['web']['volumes'].append({'type': 'bind', 'source': '/var/run/docker.sock',
                                                 'target': '/var/run/docker.sock'})
    errors = validate_compose(broken)
    assert 'RUNTIME_IDENTITY_INVALID:web' in errors
    assert 'IMAGE_NOT_PINNED:web' in errors
    assert 'DOCKER_SOCKET_SCOPE_INVALID' in errors
    broken = deepcopy(config)
    del broken['services']['query-worker']['stop_grace_period']
    assert 'WORKER_STOP_GRACE_INVALID:query-worker' in validate_compose(broken)


def test_nginx_supports_read_only_root_filesystem():
    nginx = (Path(__file__).resolve().parents[2] / 'deploy/nginx.conf').read_text(encoding='utf-8')
    assert validate_nginx(nginx) == []
    broken = nginx.replace('fastcgi_temp_path /tmp/fastcgi;\n', '')
    assert validate_nginx(broken) == ['NGINX_TEMP_PATH_INVALID:fastcgi_temp_path']
