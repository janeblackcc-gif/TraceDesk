"""Reproducible, isolated synthetic regression, not a generalization benchmark."""
from __future__ import annotations
import hashlib
import json
import math
import platform
import statistics
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from .service import ROOT, Service

NOTICE = ('40题为同作者原创合成题，非独立盲测、非真实用户数据。'
          '本报告只覆盖证据模式；未运行真实嵌入模型、大模型语义忠实性或真实用户可用性评测。')

def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0
    vals = sorted(values)
    return vals[min(len(vals) - 1, math.ceil(len(vals) * percent) - 1)]

def evaluate(output: Path | None = None) -> dict:
    dataset_path = ROOT / 'eval/questions.jsonl'
    dataset = dataset_path.read_bytes()
    rows = [json.loads(line) for line in dataset.decode('utf-8').splitlines() if line]
    config = {'profile': 'evidence', 'rrf_k': 60, 'top_k': 5, 'bm25_k1': 1.5, 'bm25_b': .75,
              'coverage_gate': .12, 'corpus': 'original fictional Atlas v1/v2', 'split': '23 dev / 17 test'}
    report = {'created_at_utc': datetime.now(timezone.utc).isoformat(), 'notice': NOTICE,
              'dataset_sha256': hashlib.sha256(dataset).hexdigest(), 'config': config,
              'config_sha256': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
              'runtime': {'python': platform.python_version(), 'os': platform.system()}, 'methods': {}, 'rows': []}
    corpus_hash = hashlib.sha256()
    for path in sorted((ROOT / 'demo').glob('*')):
        corpus_hash.update(path.name.encode()); corpus_hash.update(path.read_bytes())
    report['corpus_sha256'] = corpus_hash.hexdigest()
    with tempfile.TemporaryDirectory() as temp:
        service = Service(Path(temp) / 'eval.db')
        service.load_demo()
        for method in ('bm25', 'hybrid'):
            outcomes = []
            for item in rows:
                result = service.ask(item['question'], item['collection'], item['version'], method=method)
                all_candidates = service.store.candidates(item['collection'], item['version'])
                gold_ids = {c['id'] for c in all_candidates if any(q in c['text'] for q in item['gold_quotes'])}
                if item['kind'] == 'answerable' and not gold_ids:
                    raise ValueError(f"Unresolvable gold for {item['id']}")
                retrieved = result['sources']
                relevant_ranks = [i + 1 for i, c in enumerate(retrieved) if c['id'] in gold_ids]
                recall = len(relevant_ranks) / len(gold_ids) if gold_ids else None
                rr = 1 / min(relevant_ranks) if relevant_ranks else 0
                dcg = sum(1 / math.log2(r + 1) for r in relevant_ranks)
                ideal = sum(1 / math.log2(r + 1) for r in range(1, min(len(gold_ids), 5) + 1))
                outcome = {'id': item['id'], 'split': item['split'], 'kind': item['kind'], 'method': method,
                           'question': item['question'], 'version': item['version'], 'status': result['status'],
                           'recall_at_5': recall, 'rr_at_5': rr if gold_ids else None,
                           'ndcg_at_5': dcg / ideal if ideal else None,
                           'correct_refusal': result['status'] == 'no_evidence' if item['kind'] == 'unanswerable' else None,
                           'scope_blocked': result['status'] == 'needs_scope' if item['kind'] == 'scope' else None,
                           'scope_leaks': sum(c['version'] != item['version'] or c['collection'] != item['collection'] for c in retrieved),
                           'latency_ms': result['latency_ms'], 'retrieved': [c['id'] for c in retrieved]}
                outcomes.append(outcome)
            aggregates = {}
            for split in ('all', 'dev', 'test'):
                group = [r for r in outcomes if split == 'all' or r['split'] == split]
                positive = [r for r in group if r['kind'] == 'answerable']
                negative = [r for r in group if r['kind'] == 'unanswerable']
                scope = [r for r in group if r['kind'] == 'scope']
                mean = lambda values: round(statistics.mean(values), 4) if values else None
                refused = [r for r in group if r['status'] == 'no_evidence']
                aggregates[split] = {'count': len(group), 'answerable_count': len(positive),
                    'recall_at_5': mean([r['recall_at_5'] for r in positive]),
                    'mrr_at_5': mean([r['rr_at_5'] for r in positive]),
                    'ndcg_at_5': mean([r['ndcg_at_5'] for r in positive]),
                    'refusal_recall': mean([float(r['correct_refusal']) for r in negative]),
                    'refusal_precision': mean([float(r['kind'] == 'unanswerable') for r in refused]),
                    'scope_block_rate': mean([float(r['scope_blocked']) for r in scope]),
                    'scope_leaks': sum(r['scope_leaks'] for r in group),
                    'latency_p50_ms': percentile([r['latency_ms'] for r in group], .50),
                    'latency_p95_ms': percentile([r['latency_ms'] for r in group], .95)}
            report['methods'][method] = aggregates
            report['rows'].extend(outcomes)
        service.store.close()
    report['not_run'] = ['real_ollama_embeddings', 'real_ollama_generation', 'LLM_judge_faithfulness',
                         'cross_encoder_reranking', 'target_windows_macos_hardware', 'independent_user_trials']
    challenge_path = ROOT / 'evidence/challenge_report.json'
    if challenge_path.exists():
        challenge = json.loads(challenge_path.read_text('utf-8'))
        report['challenge'] = {k: challenge[k] for k in ('notice', 'total', 'passed', 'paraphrase', 'hard_negative')}
        report['challenge']['note'] = '随包记录；重跑主评测不会重跑困难集。困难集请单独执行 scripts/run_challenge.py。'
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report

if __name__ == '__main__':
    result = evaluate(ROOT / 'evidence/evaluation_report.json')
    print(json.dumps({'notice': result['notice'], 'methods': result['methods']}, ensure_ascii=False, indent=2))
