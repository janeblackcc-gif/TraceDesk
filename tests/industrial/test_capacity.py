import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.operations.capacity import REQUIRED_SCENARIOS, evaluate_capacity


def write_run(root, *, slow=False, scenarios=REQUIRED_SCENARIOS):
    root.mkdir()
    start = datetime.now(timezone.utc).replace(microsecond=0)
    finish = start + timedelta(minutes=31)
    environment = root / 'environment.json'
    pin = lambda value, digit: f'registry.example/{value}:1@sha256:' + digit * 64
    environment.write_text(json.dumps({'captured_at': start.isoformat(), 'scope': 'target-release',
        'os': 'Linux', 'kernel': 'fixture',
        'cpu': 'fixture-cpu', 'ram_bytes': 32_000_000_000, 'gpu': 'fixture-gpu', 'vram_bytes': 8_000_000_000,
        'gpu_driver': 'fixture', 'docker_version': '29', 'compose_version': '5', 'python_version': '3.13',
        'image_digests': {'app': pin('app', '1'), 'parse-worker': pin('worker', '2'),
                          'parser': pin('parser', '3'), 'proxy': pin('proxy', '4')},
        'postgres_version': '18.6', 'pgvector_version': '0.8.6',
        'model_provider': 'vllm', 'model_runtime_version': 'fixture',
        'embedding_model': 'fixture-embedding', 'embedding_digest': '5' * 64,
        'generation_model': 'fixture-generation', 'generation_digest': '7' * 64,
        'disk_total_bytes': 200_000_000_000,
        'timezone': 'UTC', 'code_ref': 'fixture', 'config_sha256': '6' * 64}), encoding='utf-8')
    rows = []
    steady = sorted({'steady_query', 'parse_with_query', 'reindex_with_query', 'steady_observation'} & set(scenarios))
    for index in range(60):
        timestamp = start + timedelta(seconds=index * 30)
        for operation, value in {'api': 100, 'evidence': 800, 'queue': 200, 'rag': 5000}.items():
            rows.append({'timestamp': timestamp.isoformat(), 'scenario': steady[index % len(steady)],
                         'operation': operation, 'ok': True,
                         'latency_ms': 35000 if slow and operation == 'rag' else value})
        rows.append({'timestamp': timestamp.isoformat(), 'scenario': 'steady_observation', 'operation': 'resource',
                     'ok': True, 'rss_bytes': 1_000_000 + index * 1000, 'vram_bytes': 2_000_000 + index * 1000,
                     'query_queue_depth': 2})
    for scenario in set(scenarios) - set(steady):
        rows.append({'timestamp': (start + timedelta(minutes=10)).isoformat(), 'scenario': scenario,
                     'operation': 'control', 'ok': True})
    for scenario, code in [('model_restart', 'MODEL_CONNECTION_FAILED'), ('database_restart', 'DATABASE_UNAVAILABLE'),
                           ('queue_full', 'QUEUE_FULL')]:
        if scenario in scenarios:
            rows.append({'timestamp': (start + timedelta(minutes=11)).isoformat(), 'scenario': scenario,
                         'operation': 'control', 'ok': False, 'error_code': code, 'query_queue_depth': 20})
            if scenario != 'queue_full':
                rows.append({'timestamp': (start + timedelta(minutes=12)).isoformat(), 'scenario': scenario,
                             'operation': 'control', 'ok': True})
    rows.append({'timestamp': finish.isoformat(), 'scenario': 'steady_observation', 'operation': 'resource',
                 'ok': True, 'rss_bytes': 1_060_000, 'vram_bytes': 2_060_000, 'query_queue_depth': 1})
    samples = root / 'samples.jsonl'
    samples.write_text('\n'.join(json.dumps(row) for row in rows) + '\n', encoding='utf-8')
    digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = {'format_version': 1, 'run_id': 'synthetic-capacity', 'started_at': start.isoformat(),
        'finished_at': finish.isoformat(), 'environment_path': environment.name,
        'environment_sha256': digest(environment), 'samples_path': samples.name, 'samples_sha256': digest(samples),
        'active_chunks': 50000, 'registered_users': 20, 'query_concurrency': 5, 'max_error_rate': .01,
        'max_rss_growth_bytes': 1_000_000, 'max_vram_growth_bytes': 1_000_000,
        'scenarios': sorted(scenarios), 'oom_events': 0, 'data_corruption_events': 0, 'scope_leaks': 0}
    (root / 'capacity_manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')


def test_formal_capacity_gate_passes_complete_bounded_fixture(tmp_path):
    run = tmp_path / 'pass'
    write_run(run)
    report = evaluate_capacity(run, formal=True)
    assert report['status'] == 'passed' and report['duration_seconds'] >= 1800
    assert report['latency']['rag']['p95_ms'] == 5000 and report['max_query_queue_depth'] == 20


def test_capacity_gate_rejects_slow_rag_and_missing_fault_scenarios(tmp_path):
    run = tmp_path / 'slow'
    write_run(run, slow=True, scenarios={'steady_query', 'steady_observation'})
    report = evaluate_capacity(run, formal=True)
    assert report['status'] == 'failed'
    assert 'RAG_P95_EXCEEDED' in report['failures'] and 'REQUIRED_SCENARIOS_MISSING' in report['failures']
