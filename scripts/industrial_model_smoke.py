"""One synthetic document through the configured local model + PG pipeline."""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alembic import command
from dotenv import dotenv_values
from sqlalchemy import create_engine

from app.api.schemas import QueryRequest
from app.config import Settings
from app.db.models import Job, KnowledgeBase, User, Workspace
from app.db.session import Database, migration_config, validate_database_url
from app.jobs.index_handler import IndexHandler
from app.jobs.parse_handler import ParseHandler
from app.jobs.query_handler import QueryHandler
from app.jobs.repository import JobRepository
from app.models.factory import create_bounded_provider
from app.models.vllm import BoundedVLLM, vllm_version_supported
from app.parsing.worker import DockerParser
from app.services.document_service import DocumentService
from app.services.query_service import QueryService
from app.storage.object_store import ObjectStore


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database-env', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--parser-image', required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'started_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'One synthetic fixture, not quality or holdout evidence', 'checks': []}
    def save():
        (args.output / 'status.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    value = dotenv_values(args.database_env, encoding='utf-8-sig', interpolate=False).get('TRACEDESK_TEST_DATABASE_URL')
    if not value:
        parser.error('TRACEDESK_TEST_DATABASE_URL is required')
    url = validate_database_url(value)
    name = 'tracedesk_test_model_' + uuid4().hex
    admin_engine = create_engine(url, isolation_level='AUTOCOMMIT', hide_parameters=True)
    with admin_engine.connect() as connection:
        connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
    database = Database(url.set(database=name).render_as_string(hide_password=False))
    settings = replace(Settings.load(), database_url=database.engine.url.render_as_string(hide_password=False), data_dir=args.output / 'data')
    try:
        with database.engine.begin() as connection:
            config = migration_config()
            config.attributes['connection'] = connection
            command.upgrade(config, 'head')
        provider = create_bounded_provider(settings, overall_seconds=90)
        report['model_provider'] = settings.model_provider
        report['embedding_identity'] = asdict(provider.identity())
        report['generation_model'] = settings.generation_model
        if settings.model_provider == 'vllm':
            if not isinstance(provider, BoundedVLLM):
                raise RuntimeError('VLLM_PROVIDER_FACTORY_INVALID')
            runtime_version = provider.runtime_version()
            if not vllm_version_supported(runtime_version):
                raise RuntimeError('MODEL_RUNTIME_UNSUPPORTED')
            report['model_runtime_version'] = runtime_version
            report['digest_verification'] = 'served_model_id_only'
        report['provider_url'] = settings.vllm_chat_url if settings.model_provider == 'vllm' else settings.ollama_url
        report['embedding_provider_url'] = (settings.vllm_embed_url if settings.model_provider == 'vllm'
                                            else settings.ollama_url)
        with database.transaction() as session:
            user = User(email='smoke@example.invalid', display_name='Synthetic fixture', is_system_admin=True)
            workspace = Workspace(name='Synthetic fixture', slug='synthetic-model-smoke')
            session.add_all([user, workspace])
            session.flush()
            kb = KnowledgeBase(workspace_id=workspace.id, name='合成操作文档', slug='smoke')
            session.add(kb)
            session.flush()
            user_id, kb_id = user.id, kb.id
        objects = ObjectStore(settings.data_dir / 'objects')
        DocumentService(database, objects).upload(user_id, kb_id, 'synthetic-deployment.md',
            '# 合成服务部署说明\n\n服务端口通过 SERVICE_PORT 环境变量配置。默认服务端口是 8088。修改配置后需要重启服务。\n'.encode(), 'smoke-upload', 'req_model_smoke')
        queue = JobRepository(database)
        handlers = {'parse': ParseHandler(database, objects, DockerParser(args.parser_image, timeout=30)),
                    'index': IndexHandler(database, lambda cancel: create_bounded_provider(
                        settings, cancel=cancel, overall_seconds=90)),
                    'query': QueryHandler(database, lambda cancel: create_bounded_provider(
                        settings, cancel=cancel, overall_seconds=90))}
        for kind in ('parse', 'index'):
            started = time.perf_counter()
            lease = queue.claim('model-smoke-' + kind, ('index', 'reindex') if kind == 'index' else (kind,))
            if lease is None:
                raise RuntimeError('No expected ' + kind + ' job')
            state = handlers[kind].run(queue, lease)
            with database.transaction() as session:
                error = session.get(Job, lease.job_id).error_code
            report['checks'].append({'kind': kind, 'state': state, 'error_code': error,
                                     'seconds': round(time.perf_counter() - started, 3)})
            save()
            if state != 'succeeded':
                raise RuntimeError(kind + ' smoke failed: ' + str(error))
        service = QueryService(database)
        accepted = service.create(user_id, QueryRequest(knowledge_base_id=kb_id, question='默认服务端口是多少？',
            profile='ollama', method='hybrid'), 'smoke-query', 'req_model_smoke')
        lease = queue.claim('model-smoke-query', ('query',))
        if lease is None:
            raise RuntimeError('No expected query job')
        state = handlers['query'].run(queue, lease)
        response = service.read(user_id, accepted.query_id)
        (args.output / 'response.json').write_text(response.model_dump_json(indent=2), encoding='utf-8')
        report['checks'].append({'kind': 'query', 'state': state, 'response_status': response.status,
                                 'timing': response.timing.model_dump()})
        if state != 'succeeded' or response.status != 'answered' or not any('8088' in claim.text for claim in response.claims):
            raise RuntimeError('MODEL_SMOKE_DID_NOT_ANSWER_FIXTURE')
        report['status'] = 'passed'
        return 0
    except Exception as exc:
        report['status'], report['error'] = 'failed', str(exc)
        raise
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
        database.close()
        with admin_engine.connect() as connection:
            connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        admin_engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
