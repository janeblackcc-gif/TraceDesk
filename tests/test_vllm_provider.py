import asyncio
import json
import threading
import time

import httpx
import pytest

from app.config import Settings
from app.models.factory import create_bounded_provider, create_provider
from app.models.vllm import BoundedVLLM, VLLM, vllm_version_supported
from app.services.errors import DomainError


def vllm_settings(tmp_path, **changes):
    values = {
        'data_dir': tmp_path,
        'model_provider': 'vllm',
        'vllm_embed_url': 'http://127.0.0.1:8001',
        'vllm_chat_url': 'http://127.0.0.1:8000',
        'embedding_model': 'Qwen/Qwen3-Embedding-0.6B',
        'generation_model': 'Qwen/Qwen3-4B-Instruct-2507',
        'embedding_model_digest': 'a' * 64,
        'generation_model_digest': 'b' * 64,
        'vllm_api_key': 'synthetic-secret',
    }
    values.update(changes)
    return Settings(**values)


def model_list(name):
    return {'object': 'list', 'data': [{'id': name, 'object': 'model', 'owned_by': 'vllm'}]}


def test_vllm_identity_embedding_protocol_and_factory(tmp_path):
    settings = vllm_settings(tmp_path)
    captured = []
    first = [1.0] + [0.0] * 1023
    second = [0.0, 1.0] + [0.0] * 1022

    def handle(request):
        captured.append(request)
        assert request.headers['authorization'] == 'Bearer synthetic-secret'
        if request.url.path == '/v1/models':
            return httpx.Response(200, json=model_list(settings.embedding_model))
        assert request.url.path == '/v1/embeddings'
        return httpx.Response(200, json={'object': 'list', 'model': settings.embedding_model, 'data': [
            {'object': 'embedding', 'index': 1, 'embedding': second},
            {'object': 'embedding', 'index': 0, 'embedding': first},
        ]})

    transport = httpx.MockTransport(handle)
    provider = create_bounded_provider(settings, transport=transport)
    assert isinstance(provider, BoundedVLLM)
    assert provider.identity().provider == 'vllm'
    assert provider.identity().digest == 'a' * 64
    assert provider.embed(['first', 'second']) == [first, second]
    payload = json.loads(captured[-1].content)
    assert payload == {'model': settings.embedding_model, 'input': ['first', 'second'],
                       'encoding_format': 'float'}
    assert isinstance(create_provider(settings), VLLM)


def test_vllm_chat_protocol_uses_json_object_and_openai_response(tmp_path):
    settings = vllm_settings(tmp_path)
    requests = []
    result = {'requirements': [{'required_fact': '默认服务端口', 'source_ids': ['E1S1'],
        'evidence_finding': '原文明确给出默认服务端口。', 'supported': True, 'answer': '8088'}]}

    def handle(request):
        requests.append(request)
        assert request.url.path == '/v1/chat/completions'
        return httpx.Response(200, json={'id': 'chatcmpl-fixture', 'object': 'chat.completion',
            'choices': [{'index': 0, 'finish_reason': 'stop',
                         'message': {'role': 'assistant', 'content': json.dumps(result, ensure_ascii=False)}}]})

    provider = BoundedVLLM(settings, transport=httpx.MockTransport(handle))
    answer = provider.generate('默认服务端口是多少？', [{
        'id': 'chunk-1', 'filename': 'deploy.md', 'version': 'v1',
        'text': '服务端口通过 SERVICE_PORT 配置，默认服务端口是 8088。',
    }])
    assert answer['claims'][0]['text'] == '默认服务端口：8088'
    assert answer['claims'][0]['citations'][0]['chunk_id'] == 'chunk-1'
    payload = json.loads(requests[-1].content)
    assert payload['model'] == settings.generation_model
    assert payload['stream'] is False
    assert payload['temperature'] == 0 and payload['max_tokens'] == 3072 and payload['seed'] == 42
    assert payload['response_format'] == {'type': 'json_object'}
    assert 'required_fact' in payload['messages'][0]['content']
    assert '禁止使用 fact' in payload['messages'][0]['content']
    assert 'requirements 数组必须包含 1 到 6 项' in payload['messages'][0]['content']
    assert '使用最少数量的 requirements' in payload['messages'][0]['content']
    assert '不得把对象与属性机械组合' in payload['messages'][0]['content']
    assert '忽略与问题实体无关的 evidence' in payload['messages'][0]['content']
    assert not {'think', 'keep_alive', 'options', 'format'} & set(payload)


def test_vllm_query_plan_and_runtime_status_use_generation_endpoint(tmp_path):
    settings = vllm_settings(tmp_path)
    calls = []

    def handle(request):
        calls.append((request.url.port, request.url.path))
        if request.url.path == '/v1/models':
            name = settings.embedding_model if request.url.port == 8001 else settings.generation_model
            return httpx.Response(200, json=model_list(name))
        if request.url.path == '/version':
            return httpx.Response(200, json={'version': '0.15.0'})
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps({'queries': ['service port configuration']})}}]})

    provider = BoundedVLLM(settings, transport=httpx.MockTransport(handle))
    assert provider.status()['ready'] is True
    assert provider.runtime_version() == '0.15.0'
    assert provider.plan_queries('服务端口在哪里配置？', 'English') == ['service port configuration']
    assert (8001, '/v1/models') in calls and (8000, '/v1/models') in calls
    assert calls[-1] == (8000, '/v1/chat/completions')


@pytest.mark.parametrize(('version', 'supported'), [
    ('0.8.4', False), ('0.8.5', True), ('0.15.0', True), ('unknown', False),
])
def test_vllm_runtime_version_floor(version, supported):
    assert vllm_version_supported(version) is supported


@pytest.mark.parametrize('response,code', [
    ({'data': [{'index': 0, 'embedding': [True] * 1024}]}, 'MODEL_VECTOR_INVALID'),
    ({'data': [{'index': 0, 'embedding': [1.0] * 1024},
               {'index': 0, 'embedding': [1.0] * 1024}]}, 'MODEL_RESPONSE_INVALID'),
])
def test_vllm_rejects_malformed_embedding_responses(tmp_path, response, code):
    provider = BoundedVLLM(vllm_settings(tmp_path), transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=response)))
    with pytest.raises(DomainError) as failure:
        provider.embed(['fixture'] if len(response['data']) == 1 else ['one', 'two'])
    assert failure.value.code == code


def test_vllm_deadline_cancel_and_http_error_taxonomy(tmp_path):
    closed = threading.Event()

    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(.02)
                yield b' '

        async def aclose(self):
            closed.set()

    async def drip(request):
        return httpx.Response(200, stream=Drip())

    provider = BoundedVLLM(vllm_settings(tmp_path), overall_seconds=.12,
                           transport=httpx.MockTransport(drip))
    started = time.monotonic()
    with pytest.raises(DomainError) as timeout:
        provider.embed(['fixture'])
    assert timeout.value.code == 'MODEL_DEADLINE_EXCEEDED' and timeout.value.retryable
    assert time.monotonic() - started < 1 and closed.is_set()

    cancel = threading.Event()

    async def cancelled(request):
        cancel.set()
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    provider = BoundedVLLM(vllm_settings(tmp_path), cancel=cancel,
                           transport=httpx.MockTransport(cancelled))
    with pytest.raises(DomainError) as failure:
        provider.embed(['fixture'])
    assert failure.value.code == 'MODEL_CANCELLED'

    provider = BoundedVLLM(vllm_settings(tmp_path), transport=httpx.MockTransport(
        lambda request: httpx.Response(429)))
    with pytest.raises(DomainError) as failure:
        provider.embed(['fixture'])
    assert failure.value.code == 'MODEL_TEMPORARILY_UNAVAILABLE' and failure.value.retryable
