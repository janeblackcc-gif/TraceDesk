"""Run the frozen one-shot generation-only transfer fixture with complete records."""
import json
import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from app.batch_evaluation import sha256
from app.service import Service
from run_baseline import RecordedOllama
from app.config import Settings

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    dataset = ROOT / 'datasets/grounding_transfer_v1'
    raw = (dataset / 'cases.json').read_bytes()
    frozen = json.loads((dataset / 'freeze.json').read_text('utf-8'))
    assert sha256(raw) == frozen['cases_sha256']
    assert sha256((dataset / 'README.md').read_bytes()) == frozen['protocol_sha256']
    cases = json.loads(raw)
    assert len(cases) == len({row['id'] for row in cases}) == 8
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    provider = RecordedOllama(Settings.load().ollama_url)
    status = {'started_at': datetime.now(timezone.utc).isoformat(), 'freeze': frozen, 'completed_cases': 0,
        'notice': 'AI-authored fixed-evidence transfer fixture; generation-only, not retrieval or human accuracy.',
        'max_seconds': 600, 'http_timeout_seconds': 120,
        'code_sha256': {p: sha256((ROOT / p).read_bytes()) for p in ['app/providers.py', 'app/service.py', 'app/citations.py']}}
    (output / 'manifest.json').write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
    deadline = time.perf_counter() + 600
    try:
        with (output / 'results.jsonl').open('x', encoding='utf-8') as handle:
            for case in cases:
                if time.perf_counter() >= deadline:
                    raise TimeoutError('Transfer probe budget exhausted')
                print(json.dumps({'event': 'case_started', 'id': case['id']}), flush=True)
                evidence = [{'id': f'{case["id"]}:{index}', 'filename': f'{case["id"]}.txt',
                    'version': 'transfer_v1', 'text': text} for index, text in enumerate(case['evidence'], 1)]
                before = len(provider.calls)
                result = provider.generate(case['question'], evidence)
                validated = Service.validate_claims(result, evidence)
                row = {'case': case, 'result': result, 'citations_valid': validated is not None,
                       'model_calls': provider.calls[before:]}
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
                handle.flush()
                status['completed_cases'] += 1
                print(json.dumps({'event': 'case_finished', 'id': case['id'], 'abstain': result['abstain']}), flush=True)
        status['status'] = 'completed'
    except Exception as exc:
        status.update(status='failed', error=repr(exc))
        raise
    finally:
        provider.client.close()
        status['finished_at'] = datetime.now(timezone.utc).isoformat()
        (output / 'status.json').write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding='utf-8')
