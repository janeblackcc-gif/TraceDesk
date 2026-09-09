from pathlib import Path
import hashlib
import json
import pytest
from app.batch_evaluation import METHODS, read_dataset, score_response, summarize
from scripts.export_baseline import export_run
from scripts.check_ops_dataset import validate
from scripts.release_check import REQUIRED, inspect_files


def release_tree(tmp_path):
    for name in REQUIRED:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('', encoding='utf-8')
    return sorted(REQUIRED)


def test_release_accepts_example_configuration_and_bundled_links(tmp_path):
    names = release_tree(tmp_path)
    (tmp_path / '.env.example').write_text('OLLAMA_URL=http://127.0.0.1:11434\n', encoding='utf-8')
    (tmp_path / 'README.md').write_text('[Deploy](docs/deployment.md#install)', encoding='utf-8')
    entries, errors = inspect_files(tmp_path, names)
    assert not errors and len(entries) == len(names)


def test_release_blocks_private_files_excluded_links_and_escape(tmp_path):
    names = release_tree(tmp_path)
    for name in ('.env', 'data/live.db', 'docs/private.md'):
        path = tmp_path / name
        path.parent.mkdir(exist_ok=True)
        path.write_text('private', encoding='utf-8')
    (tmp_path / 'README.md').write_text('[Excluded](docs/private.md)\n[Missing](missing.md)', encoding='utf-8')
    _, errors = inspect_files(tmp_path, names + ['.env', 'data/live.db', '../outside.py'])
    assert any('Private or generated' in error and '.env' in error for error in errors)
    assert any('Private or generated' in error and 'live.db' in error for error in errors)
    assert any('Unsafe source' in error for error in errors)
    assert any('excluded file' in error for error in errors)
    assert any('Broken or external' in error for error in errors)


def test_release_detects_machine_path_and_credentials(tmp_path):
    names = release_tree(tmp_path)
    for value in ('Z' + ':/private/owner', 'gh' + 'p_' + 'a' * 24):
        (tmp_path / 'README.md').write_text(value, encoding='utf-8')
        assert any('credential-like' in error for error in inspect_files(tmp_path, names)[1])


def test_dataset_preflight_does_not_modify_frozen_files():
    directory = Path(__file__).resolve().parents[1] / 'datasets/tracedesk_ops'
    before = {path: path.read_bytes() for path in directory.rglob('*') if path.is_file()}
    report = validate(directory)
    assert report['status'] == 'passed' and report['total_chunks'] == 17
    assert before == {path: path.read_bytes() for path in directory.rglob('*') if path.is_file()}


@pytest.fixture
def export_fixture(tmp_path):
    root = Path(__file__).resolve().parents[1]
    questions, _, metadata = read_dataset(root / 'datasets/tracedesk_ops')
    source = tmp_path / 'private_run'
    source.mkdir()
    rows, review = [], []
    for q in questions:
        for method in METHODS:
            case = q.model_dump()
            case.pop('gold_alternatives')  # Historical raw rows predate the defaulted field.
            response = {'collection': q.collection, 'version': q.version, 'sources': [], 'claims': [],
                        'status': 'no_evidence', 'actual_profile': 'ollama', 'retrieval_ms': 1, 'latency_ms': 2,
                        'trace_id': 'private-trace', 'conversation_id': 'private-conversation'}
            rows.append({'case': case, 'method': method, 'gold_chunk_groups': [], 'response': response,
                         'metrics': score_response(q, response, [{'gold'}] if q.kind == 'answerable' else []),
                         'model_calls': []})
            review.append({'id': q.id, 'method': method, 'correct': False, 'complete': False,
                           'all_claims_supported': True, 'notes': 'Synthetic export-validation fixture'})
    manifest = {'dataset': metadata, 'notice': 'Synthetic test', 'methods': list(METHODS),
                'method_order': 'test', 'code_sha256': {}, 'embedding': 'test-embed', 'generation': 'test-chat',
                'model_digests': {}, 'ollama_version': 'test', 'python': 'test', 'platform': 'test',
                'scope': {'collection': questions[0].collection, 'version': questions[0].version,
                          'chunk_count': 1, 'vector_count': 1, 'dimension': 1, 'model_key': 'test'},
                'source_db': 'private.db', 'setup_calls': ['must not export']}
    raw = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows).encode('utf-8')
    (source / 'results.jsonl').write_bytes(raw)
    files = {'manifest.json': manifest, 'status.json': {'status': 'completed'},
             'summary.json': {'status': 'completed', 'methods': summarize(rows)},
             'semantic_review.json': review,
             'semantic_summary.json': {'results_sha256': hashlib.sha256(raw).hexdigest()}}
    for name, value in files.items():
        (source / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    return source, tmp_path / 'public_export'


def test_export_accepts_legacy_case_defaults_and_checksums_all_public_payloads(export_fixture):
    source, destination = export_fixture
    result = export_run(source, destination)
    assert result['cases'] == 36
    manifest = json.loads((destination / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['dataset_id'] == 'tracedesk_ops'
    assert 'source_db' not in manifest and 'setup_calls' not in manifest
    exported = (destination / 'results.jsonl').read_text(encoding='utf-8')
    assert 'private-trace' not in exported and 'private-conversation' not in exported
    for line in (destination / 'SHA256SUMS').read_text(encoding='utf-8').splitlines():
        digest, name = line.split('  ', 1)
        assert hashlib.sha256((destination / name).read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('dataset', ['fastapi', '../private'])
def test_export_rejects_wrong_or_unknown_dataset_before_writing(export_fixture, dataset):
    source, destination = export_fixture
    with pytest.raises(ValueError, match='dataset'):
        export_run(source, destination, dataset)
    assert not destination.exists()


@pytest.mark.parametrize('change', ['missing', 'duplicate', 'null'])
def test_export_requires_complete_unique_filled_semantic_review(export_fixture, change):
    source, destination = export_fixture
    path = source / 'semantic_review.json'
    review = json.loads(path.read_text(encoding='utf-8'))
    if change == 'missing':
        review.pop()
    elif change == 'duplicate':
        review[-1] = review[0]
    else:
        review[0]['complete'] = None
    path.write_text(json.dumps(review), encoding='utf-8')
    with pytest.raises(ValueError, match='Semantic review'):
        export_run(source, destination)
    assert not destination.exists()


def test_export_does_not_allow_unknown_case_fields(export_fixture):
    source, destination = export_fixture
    path = source / 'results.jsonl'
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    rows[0]['case']['unknown_private_field'] = 'not part of the dataset'
    path.write_text('\n'.join(json.dumps(row) for row in rows), encoding='utf-8')
    with pytest.raises(ValueError, match='Extra inputs'):
        export_run(source, destination)
    assert not destination.exists()


def test_export_accepts_complete_declared_single_method_run(export_fixture):
    source, destination = export_fixture
    rows = [json.loads(line) for line in (source / 'results.jsonl').read_text(encoding='utf-8').splitlines()]
    rows = [row for row in rows if row['method'] == 'hybrid']
    raw = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows).encode('utf-8')
    (source / 'results.jsonl').write_bytes(raw)
    for name in ('manifest.json', 'summary.json', 'semantic_summary.json', 'semantic_review.json'):
        path = source / name
        value = json.loads(path.read_text(encoding='utf-8'))
        if name == 'manifest.json':
            value['methods'] = ['hybrid']
        elif name == 'summary.json':
            value['methods'] = summarize(rows, ('hybrid',))
        elif name == 'semantic_summary.json':
            value['results_sha256'] = hashlib.sha256(raw).hexdigest()
        else:
            value = [item for item in value if item['method'] == 'hybrid']
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    result = export_run(source, destination)
    assert result['cases'] == 12


@pytest.mark.parametrize('methods', [[], ['unknown'], ['hybrid', 'hybrid']])
def test_export_rejects_invalid_method_declaration(export_fixture, methods):
    source, destination = export_fixture
    path = source / 'manifest.json'
    value = json.loads(path.read_text(encoding='utf-8'))
    value['methods'] = methods
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError, match='supported methods'):
        export_run(source, destination)
    assert not destination.exists()
