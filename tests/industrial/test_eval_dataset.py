import hashlib
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from app.operations.eval_dataset import schema_bundle, validate_dataset


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def fixture(root, *, count=40, sealed=True):
    documents = root / 'documents'
    documents.mkdir(parents=True)
    content = '服务端口是 8088。修改配置后需要重启服务。\n'
    document = documents / 'deployment.md'
    document.write_text(content, encoding='utf-8')
    digest = hashlib.sha256(document.read_bytes()).hexdigest()
    corpus = {
        'format_version': 1,
        'corpus_id': 'authorized-fixture',
        'version': '1',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'retention_policy_reference': 'fixture-policy',
        'documents': [{
            'path': 'documents/deployment.md', 'sha256': digest, 'size_bytes': document.stat().st_size,
            'owner': 'fixture-owner', 'authorization_reference': 'fixture-approval',
            'authorized_uses': ['evaluation'],
            'authorization_expires_at': (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        }],
    }
    corpus_path = root / 'corpus_manifest.json'
    write_json(corpus_path, corpus)
    quote = '服务端口是 8088。'
    quote_hash = hashlib.sha256(quote.encode()).hexdigest()
    cases, labels = [], []
    for index in range(count):
        split = 'dev' if index < count // 2 else 'holdout'
        cases.append({'id': f'Q{index:03}', 'split': split, 'task_type': 'configuration',
            'answerability': 'answerable', 'severity': 'high', 'source_group': f'source-{split}-{index}',
            'template_group': f'template-{split}-{index}', 'question': f'端口是多少？{index}'})
        labels.append({'case_id': f'Q{index:03}', 'required_facts': ['默认端口数值'],
            'evidence_groups': [[{'document_sha256': digest, 'quote': quote, 'quote_sha256': quote_hash}]],
            'forbidden_claims': [], 'reviewer_ids': ['reviewer-a', 'reviewer-b'],
            'dispute_status': 'none', 'adjudicator_id': None})
    cases_path, labels_path = root / 'cases.jsonl', root / 'labels.private.jsonl'
    cases_path.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in cases) + '\n', encoding='utf-8')
    labels_path.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in labels) + '\n', encoding='utf-8')
    manifest = {
        'format_version': 2, 'dataset_id': 'fixture-real', 'name': 'Fixture', 'version': '1',
        'created_at': datetime.now(timezone.utc).isoformat(), 'corpus_manifest_path': corpus_path.name,
        'corpus_manifest_sha256': hashlib.sha256(corpus_path.read_bytes()).hexdigest(),
        'cases_path': cases_path.name, 'cases_sha256': hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        'labels_path': labels_path.name, 'labels_sha256': hashlib.sha256(labels_path.read_bytes()).hexdigest(),
        'sealed_holdout': sealed, 'split_policy': 'source_and_template_group',
    }
    write_json(root / 'dataset_manifest.json', manifest)
    return manifest, cases, labels


def test_formal_dataset_gate_records_only_counts_and_hashes(tmp_path):
    fixture(tmp_path)
    report = validate_dataset(tmp_path, formal=True)
    assert report['status'] == 'passed' and report['cases'] == 40 and report['holdout_cases'] == 20
    assert len(report['dataset_hash']) == 64 and 'question' not in report and 'quote' not in report


def test_schema_check_compares_json_structure_not_formatting(tmp_path):
    schema_path = tmp_path / 'schema.json'
    schema_path.write_text(json.dumps(schema_bundle(), separators=(',', ':')), encoding='utf-8')
    command = [sys.executable, 'scripts/check_eval_dataset.py', '--check-schema', str(schema_path)]

    matching = subprocess.run(command, capture_output=True, text=True, check=False)
    assert matching.returncode == 0, matching.stdout + matching.stderr

    changed = schema_bundle()
    changed['title'] = 'changed'
    schema_path.write_text(json.dumps(changed), encoding='utf-8')
    different = subprocess.run(command, capture_output=True, text=True, check=False)
    assert different.returncode == 1


def test_gate_rejects_hash_changes_leakage_missing_review_and_expired_authorization(tmp_path):
    manifest, cases, labels = fixture(tmp_path / 'hash')
    (tmp_path / 'hash' / 'cases.jsonl').write_text('{}\n', encoding='utf-8')
    with pytest.raises(ValueError, match='component hash'):
        validate_dataset(tmp_path / 'hash')

    manifest, cases, labels = fixture(tmp_path / 'leak')
    cases[-1]['source_group'] = cases[0]['source_group']
    path = tmp_path / 'leak' / 'cases.jsonl'
    path.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in cases) + '\n', encoding='utf-8')
    manifest['cases_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(tmp_path / 'leak' / 'dataset_manifest.json', manifest)
    with pytest.raises(ValueError, match='leaks'):
        validate_dataset(tmp_path / 'leak')

    manifest, cases, labels = fixture(tmp_path / 'review')
    labels[0]['reviewer_ids'] = ['reviewer-a']
    path = tmp_path / 'review' / 'labels.private.jsonl'
    path.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in labels) + '\n', encoding='utf-8')
    manifest['labels_sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_json(tmp_path / 'review' / 'dataset_manifest.json', manifest)
    with pytest.raises(ValueError, match='two distinct reviewers'):
        validate_dataset(tmp_path / 'review', formal=True)

    manifest, _, _ = fixture(tmp_path / 'expired')
    corpus_path = tmp_path / 'expired' / 'corpus_manifest.json'
    corpus = json.loads(corpus_path.read_text(encoding='utf-8'))
    corpus['documents'][0]['authorization_expires_at'] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    write_json(corpus_path, corpus)
    manifest['corpus_manifest_sha256'] = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
    write_json(tmp_path / 'expired' / 'dataset_manifest.json', manifest)
    with pytest.raises(ValueError, match='authorization has expired'):
        validate_dataset(tmp_path / 'expired')
