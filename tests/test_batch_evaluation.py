import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from app.batch_evaluation import Question, gold_targets, read_dataset, score_response, snapshot_scope, summarize
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
