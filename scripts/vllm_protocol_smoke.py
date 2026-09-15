"""Probe the configured vLLM endpoints without PostgreSQL or parser containers."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings
from app.models.vllm import BoundedVLLM, vllm_version_supported
from app.services.errors import DomainError


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--env-file', type=Path,
                        help='Optional model-only dotenv file; process environment takes precedence')
    parser.add_argument('--timeout-seconds', type=float, default=120)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report: dict[str, object] = {
        'status': 'running',
        'started_at': datetime.now(timezone.utc).isoformat(),
        'scope': 'Protocol compatibility only; not quality or capacity evidence',
        'checks': [],
    }

    def save() -> None:
        (args.output / 'status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    try:
        environment = dict(os.environ)
        if args.env_file is not None:
            if not args.env_file.is_file():
                raise ValueError('vLLM smoke env file is missing')
            for key, value in dotenv_values(args.env_file, encoding='utf-8-sig', interpolate=False).items():
                if value is not None and key not in environment:
                    environment[key] = value
        settings = Settings.load(ROOT, environment)
        if settings.model_provider != 'vllm':
            raise ValueError('TRACEDESK_MODEL_PROVIDER must be vllm for this smoke')
        provider = BoundedVLLM(settings, overall_seconds=args.timeout_seconds)
        runtime_version = provider.runtime_version()
        if not vllm_version_supported(runtime_version):
            raise DomainError('MODEL_RUNTIME_UNSUPPORTED', 503)
        report.update(model_provider='vllm', generation_model=settings.generation_model,
                      embedding_model=settings.embedding_model,
                      generation_url=settings.vllm_chat_url, embedding_url=settings.vllm_embed_url,
                      embedding_identity=asdict(provider.identity()), runtime_version=runtime_version,
                      digest_verification='served_model_id_only')
        status_started = time.perf_counter()
        state = provider.status()
        if not state.get('ready'):
            raise DomainError('MODEL_NOT_READY', 503)
        report['checks'].append({'name': 'models', 'status': 'passed',
                                 'seconds': round(time.perf_counter() - status_started, 3)})
        embed_started = time.perf_counter()
        vectors = provider.embed(['TraceDesk vLLM protocol smoke'])
        if len(vectors) != 1 or len(vectors[0]) != 1024:
            raise DomainError('MODEL_VECTOR_INVALID', 503)
        report['checks'].append({'name': 'embeddings', 'status': 'passed', 'dimension': len(vectors[0]),
                                 'seconds': round(time.perf_counter() - embed_started, 3)})
        plan_started = time.perf_counter()
        queries = provider.plan_queries('What is the configured service port?', 'English')
        if not queries:
            raise DomainError('MODEL_QUERY_PLAN_INVALID', 503)
        report['checks'].append({'name': 'query_plan', 'status': 'passed', 'query_count': len(queries),
                                 'seconds': round(time.perf_counter() - plan_started, 3)})
        generation_started = time.perf_counter()
        generated = provider.generate('默认服务端口是多少？', [{
            'id': 'protocol-smoke-chunk', 'filename': 'synthetic.md', 'version': 'v1',
            'page': 1, 'start_line': 1, 'end_line': 1,
            'text': '服务端口通过 SERVICE_PORT 配置，默认服务端口是 8088。',
        }])
        if type(generated.get('abstain')) is not bool or not isinstance(generated.get('claims'), list):
            raise DomainError('MODEL_GENERATION_INVALID', 503)
        report['checks'].append({'name': 'structured_generation', 'status': 'passed',
                                 'claim_count': len(generated['claims']),
                                 'seconds': round(time.perf_counter() - generation_started, 3)})
        report['status'] = 'passed'
        return 0
    except DomainError as exc:
        report['status'] = 'failed'
        report['error_code'] = exc.code
        report['error'] = str(exc)
        return 1
    except Exception as exc:
        report['status'] = 'failed'
        report['error'] = str(exc)
        return 1
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    raise SystemExit(main())
