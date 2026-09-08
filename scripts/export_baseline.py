"""Export completed runs of the public, frozen ops dataset without host metadata."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.batch_evaluation import METHODS, read_dataset, summarize


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def export_run(source: Path, destination: Path) -> dict:
    questions, _, metadata = read_dataset(ROOT / 'datasets/tracedesk_ops')
    manifest = read_json(source / 'manifest.json')
    status = read_json(source / 'status.json')
    for key in ('questions_sha256', 'documents_sha256'):
        if manifest['dataset'][key] != metadata[key]:
            raise ValueError('Only the reviewed public ops dataset may be exported')
    raw = (source / 'results.jsonl').read_bytes()
    raw_hash = hashlib.sha256(raw).hexdigest()
    rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
    expected = {(q.id, method) for q in questions for method in METHODS}
    if status['status'] != 'completed' or len(rows) != len(expected) or {(row['case']['id'], row['method']) for row in rows} != expected:
        raise ValueError('Expected a completed run with all question/method pairs')
    question_map = {q.id: q.model_dump() for q in questions}
    if any(row['case'] != question_map[row['case']['id']] for row in rows):
        raise ValueError('Result questions differ from the public dataset')
    semantic = read_json(source / 'semantic_summary.json')
    review = read_json(source / 'semantic_review.json')
    if semantic['results_sha256'] != raw_hash:
        raise ValueError('Semantic review is not bound to these raw results')
    summary = read_json(source / 'summary.json')
    if summary['status'] != 'completed' or summary['methods'] != summarize(rows):
        raise ValueError('Stored summary differs from results')
    public_rows = []
    for row in rows:
        if 'error' in row:
            raise ValueError('Review error text manually before public export')
        public_rows.append({
            **{key: row[key] for key in ('case', 'method', 'gold_chunk_groups', 'metrics')},
            'response': {key: value for key, value in row['response'].items()
                         if key not in {'trace_id', 'conversation_id'}},
            'model_calls': [{key: call[key] for key in ('path', 'payload', 'response', 'wall_ms')}
                            for call in row['model_calls'] if call['path'] == '/api/chat']})
    public_manifest = {key: manifest[key] for key in (
        'notice', 'methods', 'method_order', 'code_sha256', 'embedding', 'generation',
        'model_digests', 'ollama_version', 'python', 'platform')}
    public_manifest.update({
        'status': 'completed', 'completed_cases': len(rows),
        'dataset': {key: metadata[key] for key in ('documents_sha256', 'questions_sha256')},
        'scope': {key: manifest['scope'][key] for key in ('collection', 'version', 'chunk_count', 'vector_count', 'dimension', 'model_key')},
        'original_results_sha256': raw_hash,
        'export_notice': 'Records reserialized; request IDs, host paths, setup calls and embedding arrays omitted. Chat requests/replies preserved. Semantic review hash refers to the private original results; exported file hashes are below.'})
    payloads = {'results.jsonl': ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in public_rows)}
    for name, value in [('summary.json', summary), ('semantic_review.json', review), ('semantic_summary.json', semantic)]:
        payloads[name] = json.dumps(value, ensure_ascii=False, indent=2) + '\n'
    public_manifest['exported_sha256'] = {name: hashlib.sha256(text.encode('utf-8')).hexdigest() for name, text in payloads.items()}
    payloads['manifest.json'] = json.dumps(public_manifest, ensure_ascii=False, indent=2) + '\n'
    destination.mkdir(parents=True, exist_ok=False)
    for name, text in payloads.items():
        (destination / name).write_text(text, encoding='utf-8', newline='\n')
    return {'status': 'exported', 'cases': len(rows), 'files': len(payloads), 'original_results_sha256': raw_hash}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export_run(args.input, args.output), indent=2))


if __name__ == '__main__':
    main()
