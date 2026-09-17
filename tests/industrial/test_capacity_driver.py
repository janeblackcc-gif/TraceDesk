import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.operations.capacity import REQUIRED_SCENARIOS
from loadtests import capacity_runtime
from loadtests.capacity_evidence import image_references, parse_compose_ps, resolve_code_ref
from loadtests.capacity_runtime import SampleSink, parse_query_queue_depth, query_once, run_workload
from loadtests.capacity_volume import prepare_volume
from scripts.capacity_driver import TargetCredentials, capacity_document, prepare_corpus, preflight


def test_capacity_document_has_exact_parser_chunk_count():
    from app.ingest import chunks, parse

    data = capacity_document(1, chunks_per_document=2500, seed=20260916)
    assert len(data) < 1_000_000
    assert len(chunks(parse('capacity-01.md', data)[1])) == 2500


def test_prepare_corpus_records_parser_verified_shape(tmp_path):
    result = prepare_corpus(tmp_path / 'run', documents=2, chunks_per_document=3, seed=7)
    assert result['total_chunks'] == 6
    assert result['formal_shape'] is False
    assert result['parser_verified'] is True
    stored = json.loads((tmp_path / 'run' / 'corpus_manifest.json').read_text(encoding='utf-8'))
    assert stored['documents'][0]['chunk_count'] == 3
    assert len(stored['documents'][0]['sha256']) == 64


def test_preflight_runs_exact_cost_guard_commands(tmp_path):
    commands = []

    def runner(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout='ok\n', stderr='')

    result = preflight(tmp_path / 'preflight', runner=runner)
    assert result['status'] == 'passed'
    assert commands == [
        ['nvidia-smi', '--query-gpu=name,driver_version,memory.total', '--format=csv,noheader,nounits'],
        ['docker', 'compose', 'version'],
        ['docker', 'run', '--rm', '--network', 'none', '--read-only', '--cap-drop', 'ALL', 'alpine', 'true'],
    ]


def test_preflight_stops_when_a_check_fails(tmp_path):
    def runner(command, **kwargs):
        code = 1 if command[0] == 'nvidia-smi' else 0
        return subprocess.CompletedProcess(command, code, stdout='', stderr='unavailable' if code else '')

    result = preflight(tmp_path / 'preflight', runner=runner)
    assert result['status'] == 'failed'
    assert result['next_step'] == 'stop-before-billed-run'


def test_credentials_are_strict_and_never_in_evidence(tmp_path):
    path = tmp_path / 'credentials.json'
    path.write_text(json.dumps({
        'admin_email': 'admin@example.invalid',
        'admin_password': 'Admin-capacity-123!',
        'user_password': 'User-capacity-123!',
    }), encoding='utf-8')
    credentials = TargetCredentials.load(path)
    assert credentials.admin_email == 'admin@example.invalid'
    assert 'Admin-capacity-123!' not in repr(credentials)


def test_image_references_require_all_four_pinned_roles():
    digest = 'a' * 64
    app = f'local.registry/app@sha256:{digest}'
    parser = f'local.registry/parser@sha256:{digest}'
    proxy = f'local.registry/proxy@sha256:{digest}'
    config = {'services': {
        'web': {'image': app},
        'parse-worker': {'image': app, 'command': ['-m', 'worker', '--parser-image', parser]},
        'proxy': {'image': proxy},
    }}
    assert image_references(config) == {
        'app': app, 'parse-worker': app, 'parser': parser, 'proxy': proxy,
    }


def test_parse_compose_ps_accepts_json_array_and_json_lines():
    rows = [{'Service': 'web', 'State': 'running'}, {'Service': 'postgres', 'State': 'running'}]
    assert parse_compose_ps(json.dumps(rows)) == rows
    assert parse_compose_ps('\n'.join(json.dumps(row) for row in rows)) == rows


def test_release_bundle_code_ref_falls_back_when_checkout_is_absent():
    class Control:
        def run(self, command, **kwargs):
            return subprocess.CompletedProcess(command, 128, stdout='', stderr='not a git repository')

    digest = 'a' * 64
    assert resolve_code_ref(Control(), f'bundle-sha256:{digest}') == f'bundle-sha256:{digest}'


def test_prepare_volume_uses_new_bounded_loopback_file(tmp_path):
    commands = []
    findmnt_calls = 0

    def runner(command, **kwargs):
        nonlocal findmnt_calls
        commands.append(command)
        if command[0] == 'findmnt':
            findmnt_calls += 1
            if findmnt_calls == 1:
                return subprocess.CompletedProcess(command, 1, stdout='', stderr='not mounted')
            return subprocess.CompletedProcess(command, 0, stdout='/dev/loop7\n', stderr='')
        if command[0] == 'df':
            return subprocess.CompletedProcess(command, 0, stdout='Size Used Avail Mounted\n536870912 0 536870912 /srv/tracedesk/t075-data\n', stderr='')
        if command[:3] == ['sudo', '-n', 'losetup']:
            return subprocess.CompletedProcess(command, 0, stdout='/dev/loop7\n', stderr='')
        if command[:4] == ['sudo', '-n', 'test', '-e']:
            return subprocess.CompletedProcess(command, 1, stdout='', stderr='')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    result = prepare_volume(
        tmp_path / 'evidence',
        image_path=Path('/srv/tracedesk/t075-volume.img'),
        mount_point=Path('/srv/tracedesk/t075-data'),
        runner=runner,
    )
    assert result['loop_device'] == '/dev/loop7'
    assert all('--mountpoint' in command for command in commands if command[0] == 'findmnt')
    assert any(command[:4] == ['sudo', '-n', 'mkfs.ext4', '-F'] for command in commands)
    assert any(command[:4] == ['sudo', '-n', 'mount', '--options'] for command in commands)


def test_prepare_volume_rejects_loop_device_backed_by_another_image(tmp_path):
    def runner(command, **kwargs):
        if command[0] == 'findmnt':
            return subprocess.CompletedProcess(command, 0, stdout='/dev/loop7\n', stderr='')
        if command[:3] == ['sudo', '-n', 'losetup']:
            return subprocess.CompletedProcess(command, 0, stdout='/dev/loop8\n', stderr='')
        return subprocess.CompletedProcess(command, 0, stdout='', stderr='')

    with pytest.raises(RuntimeError, match='not backed by the requested image'):
        prepare_volume(
            tmp_path / 'evidence',
            image_path=Path('/srv/tracedesk/t075-volume.img'),
            mount_point=Path('/srv/tracedesk/t075-data'),
            runner=runner,
        )


class _SequenceClient:
    def __init__(self):
        self.query_reads = 0

    @staticmethod
    def response(status, payload=None):
        return httpx.Response(status, json=payload, request=httpx.Request('GET', 'https://target.invalid'))

    def request(self, method, path, **kwargs):
        if path == '/api/v1/me':
            return self.response(200, {})
        if method == 'POST':
            return self.response(202, {'query_id': '00000000-0000-0000-0000-000000000001'})
        if path.endswith('/trace'):
            return self.response(200, {'status': 'evidence_found', 'details': {}})
        self.query_reads += 1
        state = 'running' if self.query_reads == 1 else 'evidence_found'
        return self.response(200, {'status': state})


def test_query_once_records_real_operation_shapes(tmp_path):
    with SampleSink(tmp_path / 'samples.jsonl') as sink:
        result = query_once(
            _SequenceClient(), sink,
            kb_id='00000000-0000-0000-0000-000000000002',
            scenario='steady_query', question='capacity marker',
        )
    assert result['status'] == 'evidence_found'
    rows = [json.loads(line) for line in (tmp_path / 'samples.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [row['operation'] for row in rows] == ['api', 'queue', 'rag', 'evidence']
    assert all(row['ok'] for row in rows)


def test_query_queue_depth_counts_all_active_query_states():
    metrics = '\n'.join([
        'tracedesk_jobs{state="queued",type="query"} 3.0',
        'tracedesk_jobs{state="running",type="query"} 1.0',
        'tracedesk_jobs{state="retry_wait",type="query"} 2.0',
        'tracedesk_jobs{state="queued",type="parse"} 9.0',
    ])
    # Prometheus label order is not guaranteed; the parser must support both common orders.
    assert parse_query_queue_depth(metrics) == 6


def test_workload_state_machine_covers_ten_scenarios_without_waiting(monkeypatch, tmp_path):
    class Sampler:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

        def join(self):
            pass

    class Control:
        def start(self, service):
            pass

    class Sink:
        count = 123

    base = datetime(2026, 9, 17, tzinfo=timezone.utc)
    moments = iter([base, base + timedelta(seconds=1801), base + timedelta(seconds=1802)])
    monkeypatch.setattr(capacity_runtime, 'now', lambda: next(moments))
    monkeypatch.setattr(capacity_runtime, 'ResourceSampler', Sampler)
    monkeypatch.setattr(capacity_runtime, 'query_batch', lambda *args, **kwargs: [])
    for name in (
        'scenario_parse_with_query', 'scenario_reindex_with_query', 'scenario_model_restart',
        'scenario_worker_crash', 'scenario_database_restart', 'scenario_queue_full',
        'scenario_disk_pressure', 'scenario_stale_write',
    ):
        monkeypatch.setattr(capacity_runtime, name, lambda *args, **kwargs: None)
    clients = [_SequenceClient() for _ in range(20)]
    result = run_workload(
        admin=clients[0], clients=clients, sink=Sink(), control=Control(),
        kb_id='kb', data_dir=tmp_path, duration_seconds=1800,
    )
    assert set(result.scenario_counts) == REQUIRED_SCENARIOS
    assert all(count >= 1 for count in result.scenario_counts.values())
