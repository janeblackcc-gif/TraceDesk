"""Read-only validation of the frozen operational development dataset."""
from __future__ import annotations
import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.ingest import SUSPICIOUS, chunks, parse
from app.batch_evaluation import gold_targets, read_dataset

def validate(directory: Path) -> dict:
    questions, documents, metadata = read_dataset(directory)
    candidates, counts = [], {}
    for name, raw in documents.items():
        filename, pages = parse(name, raw)
        if any(SUSPICIOUS.search(page) for page in pages):
            raise ValueError(f'Document would be quarantined: {name}')
        parts = chunks(pages)
        if not parts:
            raise ValueError(f'No chunks produced: {name}')
        counts[name] = len(parts)
        candidates.extend({**part, 'id': f'{name}:{i}', 'filename': filename,
                           'collection': questions[0].collection, 'version': questions[0].version}
                          for i, part in enumerate(parts))
    if len(questions) != 12 or sum(q.kind == 'answerable' for q in questions) != 10:
        raise ValueError('Expected ten answerable and two unanswerable questions')
    checks = []
    for question in questions:
        if question.split != 'dev' or re.search(r'(?<![A-Za-z0-9_])v\d+(?:\.\d+)*(?![A-Za-z0-9_])', question.question, re.I):
            raise ValueError(f'Invalid split or version guard trigger: {question.id}')
        checks.append({'id': question.id, 'gold_chunk_groups': [sorted(group) for group in gold_targets(question, candidates)]})
    return {'status': 'passed', 'document_count': len(documents), 'chunks_by_file': counts,
            'total_chunks': sum(counts.values()), 'questions': len(questions),
            'answerable': 10, 'unanswerable': 2, 'gold_checks': checks,
            'questions_sha256': metadata['questions_sha256'],
            'documents_sha256': metadata['documents_sha256'],
            'notice': 'Read-only parser/hash/gold check. Historical source hashes are not assertions about current code. No model inference.'}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=ROOT / 'datasets/tracedesk_ops')
    args = parser.parse_args()
    print(json.dumps(validate(args.dataset), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
