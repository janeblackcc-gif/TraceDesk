"""Benchmark selected retrieval methods with the same local generator and corpus."""
from __future__ import annotations
import argparse
import csv
import json
import platform
import shutil
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.batch_evaluation import METHODS, gold_targets, read_dataset, score_response, sha256, snapshot_scope, summarize
from app.config import Settings
from app.providers import Ollama
from app.service import Service


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict | list) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


class RecordedOllama(Ollama):
    def __init__(self, base_url: str):
        super().__init__(base_url)
        self.calls = []

    def _request(self, path: str, payload: dict | None = None) -> dict:
        record = {'path': path, 'payload': payload}
        started = time.perf_counter()
        print(json.dumps({'time': now(), 'event': 'model_request_started', 'path': path}), flush=True)
        try:
            response = super()._request(path, payload)
            if path == '/api/embed':
                record['response'] = {key: value for key, value in response.items() if key != 'embeddings'}
                record['embeddings_sha256'] = sha256(json.dumps(response.get('embeddings')).encode())
            else:
                record['response'] = response
            return response
        except Exception as exc:
            record['error'] = repr(exc)
            raise
        finally:
            record['wall_ms'] = (time.perf_counter() - started) * 1000
            self.calls.append(record)
            print(json.dumps({'time': now(), 'event': 'model_request_finished', 'path': path,
                              'wall_ms': round(record['wall_ms'], 2), 'failed': 'error' in record}), flush=True)


def model_snapshot(provider: Ollama) -> dict:
    names = (provider.embedding, provider.generation)
    tags = provider._request('/api/tags')['models']
    selected = {item['name']: item['digest'] for item in tags if item['name'] in names}
    if set(selected) != set(names):
        raise ValueError('Both selected local models must be installed')
    return selected


def write_report(output: Path, rows: list[dict], summary: dict, status: dict) -> None:
    write_json(output / 'summary.json', {'status': status['status'], 'notice': status['notice'], 'methods': summary})
    fields = ['id', 'method', 'kind', 'status', 'actual_profile', 'hit_at_4', 'recall_at_5', 'mrr_at_5',
              'all_gold_in_context', 'behavior_expected', 'citations_exact', 'retrieval_ms', 'latency_ms', 'error', 'semantic_correct']
    with (output / 'results.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            response, metrics = row.get('response', {}), row.get('metrics', {})
            values = {'id': row['case']['id'], 'method': row['method'], 'kind': row['case']['kind'],
                      'status': response.get('status'), 'actual_profile': response.get('actual_profile'),
                      'retrieval_ms': response.get('retrieval_ms'), 'latency_ms': response.get('latency_ms'),
                      'error': row.get('error'), **{key: metrics.get(key) for key in fields if key in metrics}}
            writer.writerow(values)
    lines = ['# 批量评测结果', '', status['notice'], '',
             '所选方法均复用 Service.ask，使用相同生成模型；评测输入来自导入知识库的只读快照。',
             '先按每种方法预热一次，再按题号轮换方法顺序。预热不计分，正式每题每方法只运行一次。', '',
             '| 方法 | Hit@4 | Recall@5 | MRR@5 | 可答题返回模型答案 | 正确拒答 | 降级 | 检索P50 ms | 总耗时P50/P95 ms |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    number = lambda value: '-' if value is None else f'{value:.2f}'
    for method, item in summary.items():
        lines.append(f"| {method} | {number(item['hit_at_4'])} | {number(item['recall_at_5'])} | {number(item['mrr_at_5'])} | "
                     f"{item['answerable_real_model_answers']}/{item['answerable']} | {item['correct_refusal_count']}/{item['unanswerable']} | "
                     f"{item['fallback_count']} | {number(item['retrieval_p50_ms'])} | {number(item['total_p50_ms'])}/{number(item['total_p95_ms'])} |")
    lines += ['', '## 指标口径', '',
              '- Hit@4：可答题前4个片段是否至少命中一个标注相关块；Recall@5：前5个片段召回的相关块比例。',
              '- MRR@5：第一个相关片段排名的倒数；gold_coverage_at_4另行检查前4块覆盖了多少条必要原文，防止多事实题只命中一部分。',
              '- generation_context_gold_coverage按实际提供给生成模型的来源计算，all_gold_in_context据此判定；旧响应缺少来源清单时按其前4块协议回退。',
              '- 返回模型答案仅表示answered且实际模式为ollama，不等于语义正确；逐字引用通过也不等于条件完整。',
              '- 拒答指标按无答案题统计；同时记录可答题误拒答和引用失败降级。',
              '- 错误题单独保留；检索均值只含可评分的可答题，分母为scored_answerable，不能隐藏errors后对外报告完整通过率。',
              '- 检索耗时包含应用范围检查、模型digest查询，以及dense/hybrid的查询嵌入；不是纯排序算法时间。',
              '- 总耗时包含生成与引用校验；预热记录单独保存，小样本单轮计时不能当作稳定性能保证。',
              '- BM25、dense、hybrid均按manifest记录的应用版本评测；分句或翻译查询、相邻上下文及生成复核均保留在完整记录中。', '',
              '## 逐题复核', '',
              'results.jsonl包含完整服务响应及Ollama调用记录；results.csv便于筛选，semantic_review.json保留逐题语义标注空位。',
              '必须依据问题、expected_answer、claims和来源原文人工或代理复核，不能把自动行为检查当成答案准确率。', '']
    (output / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    review = [{'id': row['case']['id'], 'method': row['method'], 'correct': None, 'complete': None,
               'all_claims_supported': None, 'notes': ''} for row in rows]
    write_json(output / 'semantic_review.json', review)


def main() -> int:
    settings = Settings.load()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=ROOT / 'datasets/tracedesk_ops')
    parser.add_argument('--source-db', type=Path, default=settings.data_dir / 'tracedesk.db')
    parser.add_argument('--ollama-url', default=settings.ollama_url)
    parser.add_argument('--output', type=Path, default=ROOT / 'evidence/baselines' / datetime.now().strftime('tracedesk_ops_%Y%m%d_%H%M%S'))
    parser.add_argument('--max-seconds', type=int, default=900)
    parser.add_argument('--methods', nargs='+', choices=METHODS, default=list(METHODS),
                        help='Retrieval methods to run; defaults to all three')
    parser.add_argument('--notice', help='Describe this run, e.g. development regression of previously reviewed questions')
    args = parser.parse_args()
    methods = tuple(args.methods)
    if len(methods) != len(set(methods)):
        parser.error('--methods must not contain duplicates')
    if args.max_seconds < 120:
        parser.error('--max-seconds must allow at least one 120-second model request')
    questions, documents, dataset_meta = read_dataset(args.dataset)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    snapshot_dir = output / 'snapshot'
    (snapshot_dir / 'documents').mkdir(parents=True)
    for name, raw in documents.items():
        (snapshot_dir / 'documents' / name).write_bytes(raw)
    question_filename = dataset_meta['questions_path']
    shutil.copy2(args.dataset / question_filename, snapshot_dir / question_filename)
    shutil.copy2(args.dataset / 'provenance.json', snapshot_dir / 'provenance.json')
    for filename, expected_hash in dataset_meta['supplemental_sha256'].items():
        raw = (args.dataset / filename).read_bytes()
        if sha256(raw) != expected_hash:
            raise ValueError(f'Supplemental artifact changed before snapshot: {filename}')
        (snapshot_dir / filename).write_bytes(raw)
    status = {'status': 'preparing', 'started_at': now(), 'completed_cases': 0,
              'expected_cases': len(questions) * len(methods), 'output': str(output),
              'notice': args.notice or dataset_meta['provenance'].get('notice',
                  '本轮为AI辅助整理的真实项目运维资料与AI拟定开发题；不是独立测试集或真实用户评测。'),
              'http_timeout_seconds': 120, 'max_silence_seconds_per_request': 120,
              'max_seconds': args.max_seconds, 'methods': list(methods),
              'method_order': 'rotate selected methods by question index; each case gets a new conversation',
              'dataset': dataset_meta, 'semantic_review': 'pending',
              'python': platform.python_version(), 'platform': platform.platform(),
              'code_sha256': {name: sha256((ROOT / name).read_bytes()) for name in (
                  'app/__init__.py', 'app/evaluation.py', 'app/batch_evaluation.py', 'scripts/run_baseline.py', 'app/service.py',
                  'app/retrieval.py', 'app/providers.py', 'app/store.py', 'app/ingest.py', 'app/config.py', 'app/citations.py')}}
    rows, provider, service = [], None, None
    deadline = time.perf_counter() + args.max_seconds
    code = 1
    with (output / 'events.jsonl').open('x', encoding='utf-8') as events:
        def emit(event: str, **details) -> None:
            record = {'time': now(), 'event': event, **details}
            events.write(json.dumps(record, ensure_ascii=False) + '\n')
            events.flush()
            print(json.dumps(record, ensure_ascii=False), flush=True)
            write_json(output / 'status.json', status)

        try:
            emit('preparing', cases=status['expected_cases'])
            provider = RecordedOllama(args.ollama_url)
            initial_models = model_snapshot(provider)
            status.update(embedding=provider.embedding, generation=provider.generation,
                          model_digests=initial_models, ollama_url=provider.base,
                          ollama_version=provider._request('/api/version')['version'])
            key = f'{provider.embedding}@{initial_models[provider.embedding]}'
            service = Service(output / 'evaluation.db', provider)
            status['scope'] = snapshot_scope(args.source_db, service, questions, documents, key)
            candidates = service.store.candidates(questions[0].collection, questions[0].version)
            targets = {question.id: gold_targets(question, candidates) for question in questions}
            status['setup_calls'] = list(provider.calls)
            write_json(output / 'manifest.json', status)
            warmup = []
            for method in methods:
                if time.perf_counter() >= deadline:
                    raise TimeoutError('Run time budget exhausted during warmup')
                status.update(status='warming', current_case=questions[0].id, current_method=method)
                emit('warmup_started', method=method)
                before = len(provider.calls)
                result = service.ask(questions[0].question, questions[0].collection, questions[0].version,
                                     profile='ollama', method=method)
                warmup.append({'method': method, 'case_id': questions[0].id,
                               'response': result, 'model_calls': provider.calls[before:]})
                write_json(output / 'warmup.json', warmup)
            with (output / 'results.jsonl').open('x', encoding='utf-8') as handle:
                for index, question in enumerate(questions):
                    offset = index % len(methods)
                    order = methods[offset:] + methods[:offset]
                    for method in order:
                        if time.perf_counter() >= deadline:
                            raise TimeoutError('Run time budget exhausted; partial results were retained')
                        status.update(status='running', current_case=question.id, current_method=method)
                        emit('case_started', id=question.id, method=method, completed=len(rows))
                        row = {'case': question.model_dump(), 'method': method, 'started_at': now(),
                               'gold_chunk_groups': [sorted(group) for group in targets[question.id]]}
                        before = len(provider.calls)
                        try:
                            response = service.ask(question.question, question.collection, question.version,
                                                   profile='ollama', method=method)
                            row['response'] = response
                            if response['model_key'] != key:
                                raise ValueError('Embedding model changed during this run')
                            row['metrics'] = score_response(question, response, targets[question.id])
                        except Exception as exc:
                            row.update(error=repr(exc), traceback=traceback.format_exc())
                        row.update(finished_at=now(), model_calls=provider.calls[before:])
                        rows.append(row)
                        handle.write(json.dumps(row, ensure_ascii=False) + '\n')
                        handle.flush()
                        status['completed_cases'] = len(rows)
                        emit('case_finished', id=question.id, method=method,
                             status=row.get('response', {}).get('status'),
                             behavior_expected=row.get('metrics', {}).get('behavior_expected'), error=row.get('error'))
            if model_snapshot(provider) != initial_models:
                raise ValueError('Model digests changed during the run; results are not comparable')
            status['status'] = 'completed_with_errors' if any('error' in row for row in rows) else 'completed'
            code = 2 if status['status'] == 'completed_with_errors' else 0
        except KeyboardInterrupt:
            status.update(status='interrupted', error='Interrupted by the operator; partial results retained')
            code = 130
        except Exception as exc:
            status.update(status='failed', error=repr(exc), traceback=traceback.format_exc())
        finally:
            if service is not None:
                service.store.close()
            if provider is not None:
                provider.client.close()
            status.update(finished_at=now(), completed_cases=len(rows), exit_code=code)
            write_report(output, rows, summarize(rows, methods), status)
            emit('run_finished', status=status['status'], exit_code=code, completed_cases=len(rows), output=str(output))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
