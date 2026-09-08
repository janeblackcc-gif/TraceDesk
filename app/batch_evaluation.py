"""Dataset validation and metrics for the real-model batch evaluator."""
from __future__ import annotations
import hashlib
import json
import math
import sqlite3
import statistics
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .evaluation import percentile
from .service import Service

METHODS = ('bm25', 'dense', 'hybrid')


def validate_filename(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or value in {'.', '..'} or any(
            character in value for character in ('/', '\\', ':', '\0')):
        raise ValueError('Expected a single source filename without a path')
    return value


class GoldPassage(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    source_file: str
    quote: str

    @model_validator(mode='after')
    def check_passage(self) -> GoldPassage:
        validate_filename(self.source_file)
        if len(self.quote.strip()) < 8:
            raise ValueError('Each gold quote must contain at least eight non-edge-whitespace characters')
        return self


class Question(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(min_length=1)
    split: Literal['dev', 'test']
    kind: Literal['answerable', 'unanswerable']
    collection: str = Field(min_length=1, max_length=60)
    version: str = Field(min_length=1, max_length=60)
    question: str = Field(min_length=1, max_length=1000)
    expected_answer: str = Field(min_length=1)
    source_file: str | None
    gold_quotes: list[str]
    gold_alternatives: list[list[GoldPassage]] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_gold(self) -> Question:
        if self.kind == 'answerable':
            if not self.source_file or not self.gold_quotes:
                raise ValueError('Answerable questions require a source filename and gold quotes')
            validate_filename(self.source_file)
            if any(len(quote.strip()) < 8 for quote in self.gold_quotes):
                raise ValueError('Each gold quote must contain at least eight non-edge-whitespace characters')
            if self.gold_alternatives and len(self.gold_alternatives) != len(self.gold_quotes):
                raise ValueError('Gold alternatives must have one group per required gold quote')
        elif self.source_file is not None or self.gold_quotes or self.gold_alternatives:
            raise ValueError('Unanswerable questions must have no source_file, gold_quotes or gold_alternatives')
        if any(not text.strip() for text in (self.id, self.collection, self.version, self.question, self.expected_answer)):
            raise ValueError('Question fields cannot be whitespace-only')
        return self


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_dataset(directory: Path) -> tuple[list[Question], dict[str, bytes], dict]:
    provenance = json.loads((directory / 'provenance.json').read_text(encoding='utf-8-sig'))
    question_filename = provenance['questions'].get('path', 'questions.dev.jsonl')
    # The frozen original corpus used this repository-relative metadata value.
    # Preserve its bytes: map only this exact legacy label to a local filename;
    # never resolve a supplied directory prefix or accept other nested paths.
    if question_filename == 'datasets/tracedesk_ops/questions.dev.jsonl':
        question_filename = 'questions.dev.jsonl'
    question_filename = validate_filename(question_filename)
    question_path = directory / question_filename
    if question_path.resolve().parent != directory.resolve():
        raise ValueError('Question filename resolves outside the dataset directory')
    question_bytes = question_path.read_bytes()
    questions = [Question.model_validate(json.loads(line)) for line in question_bytes.decode('utf-8-sig').splitlines() if line.strip()]
    if not questions or len({q.id for q in questions}) != len(questions):
        raise ValueError('Question IDs must be nonempty and unique')
    scopes = {(q.collection, q.version) for q in questions}
    if len(scopes) != 1:
        raise ValueError('A batch must use one collection and version')
    documents = {path.name: path.read_bytes() for path in sorted((directory / 'documents').iterdir())
                 if path.is_file() and path.suffix.lower() in {'.md', '.txt', '.pdf'}}
    if not documents:
        raise ValueError('No supported documents found in the dataset')
    if (provenance['collection'], provenance['version']) != next(iter(scopes)):
        raise ValueError('Dataset provenance scope differs from the questions')
    expected = {Path(item['path']).name: item['sha256'] for item in provenance['documents']}
    observed = {name: sha256(raw) for name, raw in documents.items()}
    if observed != expected or sha256(question_bytes) != provenance['questions']['sha256']:
        raise ValueError('Dataset changed since provenance was recorded; review it and regenerate provenance first')
    if any(q.source_file not in documents for q in questions if q.kind == 'answerable'):
        raise ValueError('A question refers to an unavailable source document')
    if any(passage.source_file not in documents for q in questions
           for group in q.gold_alternatives for passage in group):
        raise ValueError('A gold alternative refers to an unavailable source document')
    supplemental = {}
    expected_artifacts = []
    if 'rubric' in provenance:
        rubric = provenance['rubric']
        expected_artifacts.append((validate_filename(rubric['path']), rubric['sha256']))
    for field, filename in (('protocol_sha256', 'README.md'), ('sources_sha256', 'sources.json')):
        if field in provenance:
            expected_artifacts.append((filename, provenance[field]))
    for filename, expected_hash in expected_artifacts:
        path = directory / filename
        if path.resolve().parent != directory.resolve():
            raise ValueError('Supplemental filename resolves outside the dataset directory')
        if not path.is_file() or sha256(path.read_bytes()) != expected_hash:
            raise ValueError(f'Supplemental artifact hash mismatch or missing file: {filename}')
        supplemental[filename] = expected_hash
    license_path = directory / 'UPSTREAM_LICENSE'
    if license_path.exists():
        if license_path.resolve().parent != directory.resolve() or not license_path.is_file():
            raise ValueError('UPSTREAM_LICENSE must be a file inside the dataset directory')
        # No upstream-license hash is required by legacy provenance. Record the
        # observed bytes so the snapshot copy is checked against this exact read.
        supplemental['UPSTREAM_LICENSE'] = sha256(license_path.read_bytes())
    return questions, documents, {'provenance': provenance, 'documents_sha256': observed,
                                   'questions_sha256': sha256(question_bytes), 'questions_path': question_filename,
                                   'supplemental_sha256': supplemental}


def snapshot_scope(source_db: Path, service: Service, questions: list[Question],
                   documents: dict[str, bytes], model_key: str) -> dict:
    """Read only the requested scope and reuse its verified vectors in a new DB."""
    collection, version = questions[0].collection, questions[0].version
    connection = sqlite3.connect(source_db.resolve().as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('BEGIN')
        live_docs = [dict(row) for row in connection.execute(
            'SELECT id,filename,sha256,status FROM documents WHERE collection=? AND version=? ORDER BY filename',
            (collection, version))]
        expected = {name: sha256(raw) for name, raw in documents.items()}
        if {doc['filename']: doc['sha256'] for doc in live_docs} != expected:
            raise ValueError('Live knowledge-base files do not exactly match this dataset and scope')
        if any(doc['status'] != 'ready' for doc in live_docs):
            raise ValueError('The dataset includes a quarantined document')
        live_chunks = [dict(row) for row in connection.execute('''
            SELECT c.* FROM chunks c JOIN documents d ON d.id=c.doc_id
            WHERE d.collection=? AND d.version=? ORDER BY c.id''', (collection, version))]
        live_vectors = [dict(row) for row in connection.execute('''
            SELECT v.chunk_id,v.value FROM vectors v JOIN chunks c ON c.id=v.chunk_id
            JOIN documents d ON d.id=c.doc_id
            WHERE d.collection=? AND d.version=? AND v.model_key=? ORDER BY v.chunk_id''',
            (collection, version, model_key))]
    finally:
        connection.close()
    if not live_chunks or {row['chunk_id'] for row in live_vectors} != {row['id'] for row in live_chunks}:
        raise ValueError('The selected model index is missing or incomplete; rebuild this scope first')
    vectors = {row['chunk_id']: json.loads(row['value']) for row in live_vectors}
    dimensions = set()
    for vector in vectors.values():
        if not isinstance(vector, list) or not vector or any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in vector):
            raise ValueError('Invalid persisted embedding vector')
        dimensions.add(len(vector))
    if len(dimensions) != 1:
        raise ValueError('Persisted vectors have inconsistent dimensions')
    for name, raw in documents.items():
        imported = service.store.import_document(name, raw, collection, version)
        if imported['status'] != 'ready':
            raise ValueError(f'Document was quarantined while reproducing the corpus: {name}')
    copied_chunks = service.store.candidates(collection, version)
    fields = ('id', 'doc_id', 'page', 'start_line', 'end_line', 'heading', 'text')
    signature = lambda rows: sorted(tuple(row[field] for field in fields) for row in rows)
    if signature(live_chunks) != signature(copied_chunks):
        raise ValueError('Current parser output differs from the live corpus; reimport documents before benchmarking')
    service.store.save_vectors(vectors, model_key)
    return {'collection': collection, 'version': version, 'documents': live_docs,
            'chunk_count': len(copied_chunks), 'vector_count': len(vectors),
            'dimension': next(iter(dimensions)), 'model_key': model_key,
            'chunks_sha256': sha256(json.dumps(signature(copied_chunks), ensure_ascii=False).encode('utf-8')),
            'vectors_sha256': sha256(json.dumps(live_vectors, ensure_ascii=False).encode('utf-8')),
            'source_db_read_only': True}


def gold_targets(question: Question, candidates: list[dict]) -> list[set[str]]:
    targets = []
    for index, quote in enumerate(question.gold_quotes):
        passages = [(question.source_file, quote)]
        if question.gold_alternatives:
            passages.extend((passage.source_file, passage.quote) for passage in question.gold_alternatives[index])
        group_matches = set()
        for filename, passage_quote in passages:
            matches = {chunk['id'] for chunk in candidates if chunk['filename'] == filename and
                       chunk['collection'] == question.collection and chunk['version'] == question.version
                       and passage_quote in chunk['text']}
            if not matches:
                raise ValueError(f'Gold quote is not present in a source chunk: {question.id}, {filename}, {passage_quote!r}')
            group_matches.update(matches)
        targets.append(group_matches)
    return targets


def score_response(question: Question, response: dict, targets: list[set[str]]) -> dict:
    sources = response['sources']
    if (response['collection'], response['version']) != (question.collection, question.version):
        raise ValueError('Response scope differs from the question')
    if any((source['collection'], source['version']) != (question.collection, question.version) for source in sources):
        raise ValueError('A retrieved source leaked across the evaluation scope')
    ids = [source['id'] for source in sources]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate chunk IDs in retrieval results')
    relevant = set().union(*targets) if targets else set()
    ranks = [index + 1 for index, chunk_id in enumerate(ids[:5]) if chunk_id in relevant]
    top4, top5 = set(ids[:4]), set(ids[:5])
    coverage = sum(bool(target & top4) for target in targets) / len(targets) if targets else None
    refusal = response['status'] == 'no_evidence' and not response['claims']
    answered = response['status'] == 'answered' and response['actual_profile'] == 'ollama' and bool(response['claims'])
    quoted = (Service.validate_claims({'abstain': False, 'claims': response['claims']}, sources[:4]) is not None
              if response['claims'] else None)
    return {'hit_at_4': float(bool(relevant & top4)) if relevant else None,
            'recall_at_5': len(relevant & top5) / len(relevant) if relevant else None,
            'mrr_at_5': 1 / min(ranks) if ranks else (0.0 if relevant else None),
            'gold_coverage_at_4': coverage,
            'all_gold_in_context': coverage == 1 if targets else None,
            'real_model_answer': answered, 'refused': refusal,
            'behavior_expected': answered if question.kind == 'answerable' else refusal,
            'correct_refusal': refusal if question.kind == 'unanswerable' else None,
            'false_refusal': refusal if question.kind == 'answerable' else None,
            'visible_fallback': response['actual_profile'] == 'evidence',
            'citations_exact': quoted, 'scope_valid': True,
            'semantic_correct': None, 'semantic_review': 'pending'}


def summarize(rows: list[dict], methods: tuple[str, ...] = METHODS) -> dict:
    summary = {}
    for method in methods:
        group = [row for row in rows if row['method'] == method]
        positive = [row for row in group if row['case']['kind'] == 'answerable']
        negative = [row for row in group if row['case']['kind'] == 'unanswerable']
        scored = [row for row in group if 'metrics' in row]
        positive_scored = [row for row in positive if 'metrics' in row]
        refused = [row for row in scored if row['metrics']['refused']]
        correct_refusals = sum(row['metrics']['correct_refusal'] is True for row in scored)
        average = lambda key: statistics.mean(row['metrics'][key] for row in positive_scored) if positive_scored else None
        duration = lambda field: [row['response'][field] for row in scored]
        summary[method] = {
            'cases': len(group), 'answerable': len(positive), 'unanswerable': len(negative),
            'scored_answerable': len(positive_scored), 'errors': sum('error' in row for row in group),
            'hit_at_4': average('hit_at_4'), 'recall_at_5': average('recall_at_5'),
            'mrr_at_5': average('mrr_at_5'), 'gold_coverage_at_4': average('gold_coverage_at_4'),
            'all_gold_in_context_count': sum(row['metrics']['all_gold_in_context'] is True for row in positive_scored),
            'answerable_real_model_answers': sum(row['metrics']['real_model_answer'] for row in positive_scored),
            'behavior_expected_count': sum(row['metrics']['behavior_expected'] for row in scored),
            'correct_refusal_count': correct_refusals,
            'refusal_recall': correct_refusals / len(negative) if negative else None,
            'refusal_precision': correct_refusals / len(refused) if refused else None,
            'false_refusal_count': sum(row['metrics']['false_refusal'] is True for row in scored),
            'fallback_count': sum(row['metrics']['visible_fallback'] for row in scored),
            'citation_checked_answers': sum(row['metrics']['citations_exact'] is not None for row in scored),
            'citation_exact_answers': sum(row['metrics']['citations_exact'] is True for row in scored),
            'retrieval_p50_ms': percentile(duration('retrieval_ms'), .5) if scored else None,
            'retrieval_p95_ms': percentile(duration('retrieval_ms'), .95) if scored else None,
            'total_p50_ms': percentile(duration('latency_ms'), .5) if scored else None,
            'total_p95_ms': percentile(duration('latency_ms'), .95) if scored else None,
            'semantic_accuracy': None,
        }
    return summary
