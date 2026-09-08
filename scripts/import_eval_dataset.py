"""Create a new isolated, hash-verified evaluation corpus and local model index."""
from __future__ import annotations
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.batch_evaluation import gold_targets, read_dataset, sha256
from app.config import Settings
from app.providers import Ollama
from app.service import Service


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New directory; never reuse an existing database')
    parser.add_argument('--ollama-url', default=Settings.load().ollama_url)
    parser.add_argument('--max-seconds', type=int, default=600)
    args = parser.parse_args()
    if args.max_seconds < 120:
        parser.error('--max-seconds must allow one 120-second model request')
    questions, documents, metadata = read_dataset(args.dataset)
    args.output.mkdir(parents=True, exist_ok=False)
    status = {'status': 'preparing', 'dataset': metadata, 'http_timeout_seconds': 120,
              'max_seconds': args.max_seconds, 'embedded_chunks': 0,
              'started_at': datetime.now(timezone.utc).isoformat()}
    deadline = time.perf_counter() + args.max_seconds
    code = 1
    with (args.output / 'events.jsonl').open('x', encoding='utf-8') as events:
        def emit(event: str, **details) -> None:
            record = {'time': datetime.now(timezone.utc).isoformat(), 'event': event, **details}
            events.write(json.dumps(record, ensure_ascii=False) + '\n')
            events.flush()
            (args.output / 'status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(record, ensure_ascii=False), flush=True)

        class IndexOllama(Ollama):
            def embed(self, texts: list[str]) -> list[list[float]]:
                if time.perf_counter() >= deadline:
                    raise TimeoutError('Index preparation time budget exhausted')
                emit('embedding_started', batch_size=len(texts), completed=status['embedded_chunks'])
                vectors = super().embed(texts)
                status['embedded_chunks'] += len(vectors)
                emit('embedding_finished', completed=status['embedded_chunks'])
                return vectors

        provider = IndexOllama(args.ollama_url)
        service = Service(args.output / 'source.db', provider)
        try:
            emit('preparing', documents=len(documents), questions=len(questions))
            for name, raw in documents.items():
                result = service.store.import_document(name, raw, questions[0].collection, questions[0].version)
                if result['status'] != 'ready':
                    raise ValueError(f'Dataset document was quarantined: {name}')
            candidates = service.store.candidates(questions[0].collection, questions[0].version)
            for question in questions:
                gold_targets(question, candidates)
            status.update(status='indexing', documents=service.store.library()['documents'], chunk_count=len(candidates),
                          embedding=provider.embedding, ollama_version=provider._request('/api/version')['version'])
            initial_key = provider.model_key()
            status['index'] = service.index(questions[0].collection, questions[0].version)
            if provider.model_key() != initial_key or status['index']['model_key'] != initial_key:
                raise ValueError('Embedding model changed while building the index')
            status['status'] = 'completed'
            code = 0
        except KeyboardInterrupt:
            status.update(status='interrupted', error='Interrupted by operator')
            code = 130
        except Exception as exc:
            status.update(status='failed', error=repr(exc))
        finally:
            service.store.close()
            provider.client.close()
            status.update(finished_at=datetime.now(timezone.utc).isoformat(), exit_code=code,
                          database_sha256=sha256((args.output / 'source.db').read_bytes()))
            emit('finished', status=status['status'], exit_code=code)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
