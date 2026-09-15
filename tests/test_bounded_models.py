import asyncio
import json
import threading
import time

import httpx
import pytest

from app.config import Settings
from app.models.ollama import BoundedOllama
from app.services.errors import DomainError


def test_digest_and_dimension_are_required(tmp_path):
    settings = Settings(data_dir=tmp_path)
    def handle(request):
        if request.url.path == '/api/tags':
            return httpx.Response(200, json={'models': [{'name': settings.embedding_model, 'digest': 'a' * 64}]})
        assert json.loads(request.content)['truncate'] is False
        return httpx.Response(200, json={'embeddings': [[1.0, 0.0]]})
    provider = BoundedOllama(settings, transport=httpx.MockTransport(handle))
    assert provider.identity().digest == 'a' * 64
    with pytest.raises(DomainError, match='MODEL_VECTOR_INVALID'):
        provider.embed(['fixture'])


def test_drip_response_has_overall_deadline_and_closes_transport(tmp_path):
    closed = threading.Event()
    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            for _ in range(100):
                await asyncio.sleep(.02)
                yield b' '
        async def aclose(self):
            closed.set()
    async def handle(request):
        return httpx.Response(200, stream=Drip())
    provider = BoundedOllama(Settings(data_dir=tmp_path), overall_seconds=.12, transport=httpx.MockTransport(handle))
    started = time.monotonic()
    with pytest.raises(DomainError, match='MODEL_DEADLINE_EXCEEDED'):
        provider.embed(['fixture'])
    assert time.monotonic() - started < 1
    assert closed.is_set()


def test_cancel_during_call_and_5xx_error_taxonomy(tmp_path):
    cancel = threading.Event()
    async def handle(request):
        cancel.set()
        await asyncio.sleep(5)
        return httpx.Response(200, json={})
    provider = BoundedOllama(Settings(data_dir=tmp_path), cancel=cancel, transport=httpx.MockTransport(handle))
    with pytest.raises(DomainError, match='MODEL_CANCELLED'):
        provider.embed(['fixture'])
    provider = BoundedOllama(Settings(data_dir=tmp_path), transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    with pytest.raises(DomainError) as failure:
        provider.embed(['fixture'])
    assert failure.value.code == 'MODEL_TEMPORARILY_UNAVAILABLE' and failure.value.retryable
