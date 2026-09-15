import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.operations.acceptance import acceptance_mapping, validate_acceptance

TRACEABILITY = Path(__file__).resolve().parents[2] / 'docs/traceability.csv'


def artifact(root, name, value, kind):
    path = root / name
    path.write_text(json.dumps(value), encoding='utf-8')
    return {'kind': kind, 'path': name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


def manifest(root, *, scope='target-release'):
    mapping = acceptance_mapping(TRACEABILITY)
    pin = lambda value, digit: f'registry.example/{value}:1@sha256:' + digit * 64
    environment = artifact(root, 'environment.json', {'captured_at': datetime.now(timezone.utc).isoformat(),
        'scope': scope,
        'os': 'Linux', 'kernel': 'fixture', 'cpu': 'fixture-cpu', 'ram_bytes': 32_000_000_000,
        'gpu': 'fixture-gpu', 'vram_bytes': 8_000_000_000, 'gpu_driver': 'fixture',
        'docker_version': '29', 'compose_version': '5', 'python_version': '3.13',
        'image_digests': {'app': pin('app', '1'), 'parse-worker': pin('worker', '2'),
                          'parser': pin('parser', '3'), 'proxy': pin('proxy', '4')},
        'postgres_version': '18.6', 'pgvector_version': '0.8.6',
        'model_provider': 'vllm', 'model_runtime_version': 'fixture',
        'embedding_model': 'fixture-embedding', 'embedding_digest': '5' * 64,
        'generation_model': 'fixture-generation', 'generation_digest': '7' * 64,
        'disk_total_bytes': 200_000_000_000,
        'timezone': 'UTC', 'code_ref': 'fixture', 'config_sha256': '6' * 64}, 'environment')
    generic = artifact(root, 'integration.json', {'status': 'passed'}, 'integration')
    special = {
        'FA-01': artifact(root, 'deployment.json', {'status': 'passed', 'scope': 'production'}, 'deployment'),
        'FA-14': artifact(root, 'migration.json', {'status': 'passed', 'source_role': 'authorized_real_copy'}, 'migration'),
        'FA-15': artifact(root, 'restore.json', {'status': 'passed', 'fixture': 'target-copy',
                                                'rto_seconds': 10, 'rpo_seconds': 0}, 'restore'),
        'FA-16': artifact(root, 'upgrade.json', {'status': 'passed', 'scope': 'target',
            'previous_image': pin('app', '7'), 'new_image': pin('app', '8'),
            'migration_failure_recovered': True, 'application_failure_recovered': True}, 'upgrade'),
        'FA-18': artifact(root, 'capacity.json', {'status': 'passed', 'formal': True, 'duration_seconds': 1800}, 'capacity'),
        'FA-19': artifact(root, 'quality.json', {'status': 'passed', 'dataset_role': 'real_holdout',
            'thresholds_frozen_before_run': True, 'scope_leaks': 0, 'severe_errors': 0}, 'quality'),
        'FA-20': artifact(root, 'browser.json', {'status': 'passed'}, 'browser'),
        'FA-21': artifact(root, 'pilot.json', {'status': 'passed', 'thresholds_frozen_before_start': True,
            'real_user_count': 5, 'task_count': 30, 'high_severity_errors': 0, 'responsible_signoff': 'fixture'}, 'pilot'),
    }
    gates = []
    for number in range(1, 22):
        identifier = f'FA-{number:02}'
        gates.append({'fa_id': identifier, 'status': 'PASS',
            'requirement_ids': sorted(mapping[identifier]['requirements']),
            'task_ids': sorted(mapping[identifier]['tasks']), 'artifacts': [special.get(identifier, generic)],
            'blocker': None, 'notes': None})
    value = {'format_version': 1, 'run_id': 'fixture', 'scope': scope,
             'created_at': datetime.now(timezone.utc).isoformat(), 'environment': environment, 'gates': gates}
    path = root / 'acceptance_manifest.json'
    path.write_text(json.dumps(value, indent=2), encoding='utf-8')
    return path, value


def test_target_acceptance_requires_all_hashed_external_evidence(tmp_path):
    path, _ = manifest(tmp_path)
    report = validate_acceptance(path, tmp_path, TRACEABILITY)
    assert report['status'] == 'passed' and report['passed'] == 21 and not report['blocked']


def test_acceptance_rejects_development_quality_and_local_scope_never_releases(tmp_path):
    target = tmp_path / 'target'
    target.mkdir()
    path, value = manifest(target)
    quality = target / 'quality.json'
    quality.write_text(json.dumps({'status': 'passed', 'dataset_role': 'legacy_dev',
                                   'thresholds_frozen_before_run': False, 'scope_leaks': 0,
                                   'severe_errors': 0}), encoding='utf-8')
    for gate in value['gates']:
        if gate['fa_id'] == 'FA-19':
            gate['artifacts'][0]['sha256'] = hashlib.sha256(quality.read_bytes()).hexdigest()
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='real holdout'):
        validate_acceptance(path, target, TRACEABILITY)

    local = tmp_path / 'local'
    local.mkdir()
    local_path, _ = manifest(local, scope='local-readiness')
    report = validate_acceptance(local_path, local, TRACEABILITY)
    assert report['status'] == 'blocked' and 'LOCAL_SCOPE_NOT_RELEASE' in report['blocked']
