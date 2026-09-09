import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from app.batch_evaluation import Question, gold_targets, read_dataset, score_response, sha256, snapshot_scope, summarize
from app.service import Service
from app.store import Store


def question(**changes):
    return Question.model_validate({
        'id': 'P1', 'split': 'dev', 'kind': 'answerable', 'collection': 'ops', 'version': '2026-09-08',
        'question': 'What is the port?', 'expected_answer': 'The port is 8765.',
        'source_file': 'guide.md', 'gold_quotes': ['The port is 8765.'], **changes})


def source(chunk_id='a', **changes):
    return {'id': chunk_id, 'filename': 'guide.md', 'collection': 'ops', 'version': '2026-09-08',
            'text': 'The port is 8765.', **changes}


def response(**changes):
    return {'collection': 'ops', 'version': '2026-09-08', 'status': 'answered', 'actual_profile': 'ollama',
            'sources': [source()], 'claims': [{'text': 'Use port 8765.',
                'citations': [{'chunk_id': 'a', 'quote': 'The port is 8765.'}]}],
            'retrieval_ms': 10, 'latency_ms': 100, **changes}


def test_gold_requires_designated_document_and_scope():
    candidates = [source(), source('b', filename='other.md'), source('c', version='old'),
                  source('d', collection='other')]
    assert gold_targets(question(), candidates) == [{'a'}]
    with pytest.raises(ValueError, match='Gold quote'):
        gold_targets(question(), candidates[1:])


def test_partial_context_is_not_complete_and_rank_five_is_not_context():
    q = question(gold_quotes=['The port is 8765.', 'Back up every day.'])
    sources = [source(), *[source(str(i), text='Unrelated details.') for i in range(3)],
               source('last', text='Back up every day.')]
    metrics = score_response(q, response(sources=sources), gold_targets(q, sources))
    assert metrics['hit_at_4'] == metrics['recall_at_5'] == metrics['mrr_at_5'] == 1
    assert metrics['gold_coverage_at_4'] == .5
    assert metrics['all_gold_in_context'] is False
    assert metrics['semantic_correct'] is None


def test_expanded_generation_context_is_scored_separately_from_top_four():
    q = question(gold_quotes=['The port is 8765.', 'Back up every day.'])
    sources = [source(), *[source(str(i), text='Unrelated details.') for i in range(3)],
               source('last', text='Back up every day.')]
    result = response(sources=sources, generation_source_ids=[s['id'] for s in sources],
                      claims=[{'text':'Back up every day.',
                               'citations':[{'chunk_id':'last','quote':'Back up every day.'}]}])
    metrics = score_response(q, result, gold_targets(q, sources))
    assert metrics['gold_coverage_at_4'] == .5
    assert metrics['generation_context_gold_coverage'] == 1
    assert metrics['all_gold_in_context'] and metrics['citations_exact']


@pytest.mark.parametrize('ids', [['foreign'], ['a','a'], [['a']], 'a'])
def test_generation_context_manifest_must_reference_unique_returned_sources(ids):
    with pytest.raises(ValueError, match='Generation context'):
        score_response(question(), response(generation_source_ids=ids), [{'a'}])


def test_no_answer_has_no_retrieval_recall_and_false_refusal_is_separate():
    result = response(status='no_evidence', claims=[])
    negative = question(id='N1', kind='unanswerable', source_file=None, gold_quotes=[])
    metrics = score_response(negative, result, [])
    assert metrics['hit_at_4'] is metrics['recall_at_5'] is metrics['mrr_at_5'] is None
    assert metrics['correct_refusal'] is True and metrics['false_refusal'] is None
    positive_metrics = score_response(question(), result, [{'a'}])
    assert positive_metrics['false_refusal'] is True
    assert positive_metrics['behavior_expected'] is False


def test_exact_quotes_in_fallback_do_not_count_as_model_answer():
    metrics = score_response(question(), response(status='evidence_found', actual_profile='evidence'), [{'a'}])
    assert metrics['citations_exact'] is True and metrics['visible_fallback'] is True
    assert not metrics['real_model_answer'] and not metrics['behavior_expected'] and not metrics['refused']


def test_cross_scope_response_is_rejected():
    with pytest.raises(ValueError, match='leaked'):
        score_response(question(), response(sources=[source(version='old')]), [{'a'}])


def test_summary_reports_error_denominators_and_preserves_retrieval_misses():
    positive = question().model_dump()
    negative = question(id='N1', kind='unanswerable', source_file=None, gold_quotes=[]).model_dump()
    missed = response(status='no_evidence', sources=[], claims=[])
    refused = response(status='no_evidence', claims=[])
    rows = [
        {'method': 'bm25', 'case': positive, 'response': missed,
         'metrics': score_response(Question.model_validate(positive), missed, [{'a'}])},
        {'method': 'bm25', 'case': {**positive, 'id': 'P2'}, 'error': 'timeout'},
        {'method': 'bm25', 'case': negative, 'response': refused,
         'metrics': score_response(Question.model_validate(negative), refused, [])}]
    summary = summarize(rows)['bm25']
    assert (summary['cases'], summary['answerable'], summary['scored_answerable'], summary['errors']) == (3, 2, 1, 1)
    assert summary['hit_at_4'] == summary['recall_at_5'] == summary['mrr_at_5'] == 0
    assert summary['refusal_recall'] == 1 and summary['refusal_precision'] == .5
    assert summary['semantic_accuracy'] is None


class NoModelCalls:
    def model_key(self):
        raise AssertionError('Snapshotting must not call a model')


@pytest.fixture
def snapshot_setup(tmp_path):
    source_path = tmp_path / 'live.db'
    live = Store(source_path)
    destination = Service(tmp_path / 'snapshot.db', NoModelCalls())
    documents = {'guide.md': b'# Network\nThe port is 8765.\n\n## Backup\nBack up every day.'}
    live.import_document('guide.md', documents['guide.md'], 'ops', '2026-09-08')
    live.import_document('guide.md', b'# Old\nThe port is 9000.', 'ops', 'old')
    live.import_document('other.md', b'# Other\nOther collection data.', 'other', '2026-09-08')
    candidates = live.candidates('ops', '2026-09-08')
    vectors = {chunk['id']: [1., .5] for chunk in candidates}
    live.save_vectors(vectors, 'embed@digest')
    yield source_path, live, destination, documents, vectors
    destination.store.close()
    live.close()


def test_snapshot_reuses_only_selected_scope_without_writing_live_database(snapshot_setup):
    source_path, live, destination, documents, vectors = snapshot_setup
    before = list(live.db.iterdump())
    result = snapshot_scope(source_path, destination, [question()], documents, 'embed@digest')
    assert result['source_db_read_only'] and result['chunk_count'] == result['vector_count'] == 2
    assert len(destination.store.library()['documents']) == 1
    assert destination.store.get_vectors(list(vectors), 'embed@digest') == vectors
    assert list(live.db.iterdump()) == before


def test_snapshot_rejects_stale_index_and_changed_corpus(snapshot_setup):
    source_path, _, destination, documents, _ = snapshot_setup
    with pytest.raises(ValueError, match='missing or incomplete'):
        snapshot_scope(source_path, destination, [question()], documents, 'embed@new-digest')
    with pytest.raises(ValueError, match='exactly match'):
        snapshot_scope(source_path, destination, [question()], {'guide.md': b'Changed content'}, 'embed@digest')
    assert destination.store.library()['documents'] == []


def test_dataset_hashes_and_question_contract(tmp_path):
    root = Path(__file__).resolve().parents[1]
    questions, documents, metadata = read_dataset(root / 'datasets/tracedesk_ops')
    assert len(questions) == 12 and len(documents) == 3
    (tmp_path / 'documents').mkdir()
    for name, raw in documents.items():
        (tmp_path / 'documents' / name).write_bytes(raw)
    (tmp_path / 'questions.dev.jsonl').write_text('\n'.join(q.model_dump_json() for q in questions), encoding='utf-8')
    (tmp_path / 'provenance.json').write_text(json.dumps(metadata['provenance']), encoding='utf-8')
    with pytest.raises(ValueError, match='Dataset changed'):
        read_dataset(tmp_path)
    with pytest.raises(ValidationError):
        question(kind='unanswerable')
    with pytest.raises(ValidationError):
        question(source_file='../guide.md')


def test_alternative_documents_are_or_within_each_required_group():
    q = question(gold_quotes=['The port is 8765.', 'Back up every day.'], gold_alternatives=[
        [{'source_file': 'reference.md', 'quote': 'Listen on port 8765.'}],
        [{'source_file': 'reference.md', 'quote': 'Daily backups are required.'}]])
    candidates = [source(), source('b', text='Back up every day.'),
                  source('c', filename='reference.md', text='Listen on port 8765.'),
                  source('d', filename='reference.md', text='Daily backups are required.'),
                  source('wrong_version', filename='reference.md', version='old', text='Listen on port 8765.')]
    targets = gold_targets(q, candidates)
    assert targets == [{'a', 'c'}, {'b', 'd'}]
    partial = score_response(q, response(sources=[candidates[2]], claims=[]), targets)
    assert partial['gold_coverage_at_4'] == .5 and not partial['all_gold_in_context']
    complete = score_response(q, response(sources=candidates[2:4], claims=[]), targets)
    assert complete['gold_coverage_at_4'] == 1 and complete['all_gold_in_context']


@pytest.mark.parametrize('candidates', [
    [source()],
    [source(), source('b', filename='reference.md', text='A different passage.')],
    [source('b', filename='reference.md', text='Listen on port 8765.')],
])
def test_every_annotated_passage_must_exist_even_if_another_alternative_matches(candidates):
    q = question(gold_alternatives=[[{'source_file': 'reference.md', 'quote': 'Listen on port 8765.'}]])
    with pytest.raises(ValueError, match='Gold quote'):
        gold_targets(q, candidates)


@pytest.mark.parametrize('changes', [
    {'gold_alternatives': [[{'source_file': 'a.md', 'quote': 'Valid quote here.'}], []]},
    {'gold_alternatives': [{'source_file': 'a.md', 'quote': 'Valid quote here.'}]},
    {'gold_alternatives': [[{'source_file': '../a.md', 'quote': 'Valid quote here.'}]]},
    {'gold_alternatives': [[{'source_file': 'sub\\a.md', 'quote': 'Valid quote here.'}]]},
    {'gold_alternatives': [[{'source_file': 'a.md', 'quote': 'short'}]]},
    {'gold_alternatives': [[{'source_file': 'a.md', 'quote': 'Valid quote here.', 'extra': True}]]},
    {'gold_alternatives': [[{'source_file': 123, 'quote': 'Valid quote here.'}]]},
    {'gold_alternatives': [[{'source_file': 'a.md', 'quote': 12345678}]]},
    {'gold_alternatives': None},
    {'kind': 'unanswerable', 'source_file': None, 'gold_quotes': [], 'gold_alternatives': [[]]},
])
def test_invalid_alternative_structure_is_rejected(changes):
    with pytest.raises(ValidationError):
        question(**changes)


def write_dataset(directory, *, filename='questions.dev.jsonl', record_path=False, changes=None,
                  extra_documents=None, notice=None):
    documents = {'guide.md': b'The port is 8765.', **(extra_documents or {})}
    (directory / 'documents').mkdir(parents=True)
    for name, raw in documents.items():
        (directory / 'documents' / name).write_bytes(raw)
    raw = question(**(changes or {})).model_dump_json().encode('utf-8') + b'\n'
    (directory / filename).write_bytes(raw)
    provenance = {'collection': 'ops', 'version': '2026-09-08',
                  'documents': [{'path': name, 'sha256': sha256(content)} for name, content in documents.items()],
                  'questions': {'sha256': sha256(raw)}}
    if record_path:
        provenance['questions']['path'] = filename
    if notice is not None:
        provenance['notice'] = notice
    (directory / 'provenance.json').write_text(json.dumps(provenance), encoding='utf-8')
    return provenance, raw


@pytest.mark.parametrize('filename,record_path', [('questions.dev.jsonl', False), ('questions.test.jsonl', True)])
def test_dataset_selects_provenance_filename_and_preserves_default(tmp_path, filename, record_path):
    _, raw = write_dataset(tmp_path, filename=filename, record_path=record_path,
                          changes={'split': 'test' if record_path else 'dev'})
    questions, _, metadata = read_dataset(tmp_path)
    assert len(questions) == 1 and questions[0].gold_alternatives == []
    assert metadata['questions_path'] == filename and metadata['questions_sha256'] == sha256(raw)


def test_exact_frozen_legacy_label_maps_to_local_file_without_changing_provenance(tmp_path):
    provenance, raw = write_dataset(tmp_path)
    provenance['questions']['path'] = 'datasets/tracedesk_ops/questions.dev.jsonl'
    path = tmp_path / 'provenance.json'
    path.write_text(json.dumps(provenance), encoding='utf-8')
    frozen = path.read_bytes()
    questions, _, metadata = read_dataset(tmp_path)
    assert len(questions) == 1
    assert metadata['questions_path'] == 'questions.dev.jsonl'
    assert metadata['questions_sha256'] == sha256(raw)
    assert path.read_bytes() == frozen
    assert metadata['provenance']['questions']['path'] == provenance['questions']['path']


@pytest.mark.parametrize('filename', ['../outside.jsonl', 'nested/questions.test.jsonl',
                                     'datasets/tracedesk_ops/questions.test.jsonl',
                                     'datasets/other/questions.dev.jsonl',
                                     '..\\outside.jsonl', 'C' + ':\\outside.jsonl', '/outside.jsonl',
                                     'C:outside.jsonl', '.', '..', '', None])
def test_dataset_rejects_question_path_escape_before_reading(tmp_path, filename):
    provenance, _ = write_dataset(tmp_path)
    provenance['questions']['path'] = filename
    (tmp_path / 'provenance.json').write_text(json.dumps(provenance), encoding='utf-8')
    with pytest.raises(ValueError, match='filename'):
        read_dataset(tmp_path)


def test_dataset_checks_alternative_document_availability(tmp_path):
    write_dataset(tmp_path, changes={'gold_alternatives': [[
        {'source_file': 'missing.md', 'quote': 'Listen on port 8765.'}]]})
    with pytest.raises(ValueError, match='alternative.*unavailable'):
        read_dataset(tmp_path)


@pytest.mark.parametrize('notice', [None, 'Official documentation challenge; not used for tuning.'])
def test_runner_copies_test_filename_and_uses_provenance_notice_without_model_calls(tmp_path, monkeypatch, notice):
    from scripts import run_baseline
    dataset, output = tmp_path / 'dataset', tmp_path / 'output'
    provenance, raw = write_dataset(dataset, filename='questions.test.jsonl', record_path=True,
                                   changes={'split': 'test'}, notice=notice)
    artifacts = {'rubric.json': b'{"criteria": []}', 'README.md': b'Frozen protocol',
                 'sources.json': b'[]', 'UPSTREAM_LICENSE': b'Public upstream license'}
    for name, content in artifacts.items():
        (dataset / name).write_bytes(content)
    provenance.update(rubric={'path': 'rubric.json', 'sha256': sha256(artifacts['rubric.json'])},
                      protocol_sha256=sha256(artifacts['README.md']),
                      sources_sha256=sha256(artifacts['sources.json']))
    (dataset / 'provenance.json').write_text(json.dumps(provenance), encoding='utf-8')
    def stop_before_models(*args, **kwargs):
        raise RuntimeError('intentional stop after dataset snapshot; no model calls')
    monkeypatch.setattr(run_baseline, 'RecordedOllama', stop_before_models)
    monkeypatch.setattr('sys.argv', ['run_baseline', '--dataset', str(dataset), '--output', str(output)])
    assert run_baseline.main() == 1
    assert (output / 'snapshot/questions.test.jsonl').read_bytes() == raw
    assert not (output / 'snapshot/questions.dev.jsonl').exists()
    for name, content in artifacts.items():
        assert (output / 'snapshot' / name).read_bytes() == content
    status = json.loads((output / 'status.json').read_text(encoding='utf-8'))
    assert 'intentional stop' in status['error']
    assert status['notice'] == (notice or '本轮为AI辅助整理的真实项目运维资料与AI拟定开发题；不是独立测试集或真实用户评测。')
    assert '导入知识库的只读快照' in (output / 'report.md').read_text(encoding='utf-8')


@pytest.mark.parametrize('changed_file', ['rubric.json', 'README.md', 'sources.json'])
def test_supplemental_artifact_hash_tampering_is_rejected(tmp_path, changed_file):
    provenance, _ = write_dataset(tmp_path)
    artifacts = {'rubric.json': b'{"criteria": []}', 'README.md': b'Frozen protocol',
                 'sources.json': b'[]', 'UPSTREAM_LICENSE': b'Public upstream license'}
    for name, raw in artifacts.items():
        (tmp_path / name).write_bytes(raw)
    provenance.update(rubric={'path': 'rubric.json', 'sha256': sha256(artifacts['rubric.json'])},
                      protocol_sha256=sha256(artifacts['README.md']),
                      sources_sha256=sha256(artifacts['sources.json']))
    (tmp_path / 'provenance.json').write_text(json.dumps(provenance), encoding='utf-8')
    _, _, metadata = read_dataset(tmp_path)
    assert metadata['supplemental_sha256'] == {name: sha256(raw) for name, raw in artifacts.items()}
    (tmp_path / changed_file).write_bytes(b'tampered')
    with pytest.raises(ValueError, match='Supplemental artifact hash mismatch'):
        read_dataset(tmp_path)
