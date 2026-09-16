"""Bounded adapter for vLLM's OpenAI-compatible HTTP API."""
from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import TypeAlias

import httpx
from pydantic import JsonValue, TypeAdapter, ValidationError

from app.config import Settings, local_vllm_url
from app.providers import ModelUnavailable
from app.services.errors import DomainError
from .ollama import BoundedOllama
from .profile import EmbeddingIdentity


JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
HEX_DIGEST = re.compile(r'[0-9a-f]{64}')
VLLM_MIN_VERSION = (0, 8, 5)
VLLM_VERSION = re.compile(r'^(\d+)\.(\d+)\.(\d+)')
JSON_DICT: TypeAlias = dict[str, JsonValue]


def _structured_json_hint(schema: JSON_DICT) -> str:
    """Keep Qwen structured output on the adapter's exact field contract."""
    properties = schema.get('properties')
    if isinstance(properties, dict) and 'requirements' in properties:
        return (
            '\n输出键名必须逐字使用 required_fact、source_ids、evidence_finding、supported、answer；'
            '禁止使用 fact、finding 或其他别名。每个 requirement 必须同时包含这五个键。'
            'requirements 数组必须包含 1 到 6 项；子问题超过 6 个时合并同类事实，不得输出第 7 项。'
            '示例结构：{"requirements":[{"required_fact":"...","source_ids":[],'
            '"evidence_finding":"...","supported":false,"answer":""}]}。'
        )
    if isinstance(properties, dict) and 'queries' in properties:
        return '\n只返回合法 JSON；顶层键必须逐字为 queries，值必须是字符串数组，不要使用其他键名。'
    return '\n只返回合法 JSON；键名必须与请求 schema 的 properties 完全一致，不要使用别名。'


def vllm_version_supported(value: str) -> bool:
    """Return whether the runtime meets the candidate Qwen model minimum."""
    match = VLLM_VERSION.match(value.strip())
    if match is None:
        return False
    major, minor, patch = (int(part) for part in match.groups())
    return (major, minor, patch) >= VLLM_MIN_VERSION


class BoundedVLLM(BoundedOllama):
    """Use the existing provider contract over vLLM's OpenAI API.

    The parent class still owns prompt validation and citation assembly. This
    adapter only translates the wire protocol and keeps the same cancellation,
    deadline, and failure taxonomy used by industrial workers.
    """

    def __init__(self, settings: Settings, *, cancel: threading.Event | None = None,
                 overall_seconds: float = 120, transport: httpx.AsyncBaseTransport | None = None):
        if settings.model_provider != 'vllm':
            raise ValueError('BoundedVLLM requires TRACEDESK_MODEL_PROVIDER=vllm')
        if overall_seconds <= 0:
            raise ValueError('overall_seconds must be positive')
        embedding_digest = settings.embedding_model_digest
        generation_digest = settings.generation_model_digest
        if not embedding_digest or not generation_digest:
            raise ValueError('vLLM model digests are required')
        if not HEX_DIGEST.fullmatch(embedding_digest) or not HEX_DIGEST.fullmatch(generation_digest):
            raise ValueError('vLLM model digests must be lowercase 64-character SHA-256 values')
        self.base = local_vllm_url(settings.vllm_chat_url)
        self.embed_base = local_vllm_url(settings.vllm_embed_url)
        self.embedding, self.generation = settings.embedding_model, settings.generation_model
        self.embedding_digest, self.generation_digest = embedding_digest, generation_digest
        self.api_key = settings.vllm_api_key
        self.cancel = cancel or threading.Event()
        self.overall_seconds, self.transport = overall_seconds, transport
        self._deadline: float | None = None

    def _headers(self) -> dict[str, str]:
        headers = {'Accept': 'application/json'}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        return headers

    async def _http_raw(self, base: str, path: str, payload: JSON_DICT | None,
                        seconds: float) -> JSON_DICT:
        # The operation deadline is the total bound; do not impose a shorter
        # fixed read timeout on slow first-token/structured generations.
        timeout = httpx.Timeout(seconds, connect=min(3.0, seconds))
        async with httpx.AsyncClient(base_url=base, timeout=timeout,
                transport=self.transport, trust_env=False, follow_redirects=False) as client:
            if payload is not None:
                request = client.stream('POST', path, headers=self._headers(), json=payload)
            else:
                request = client.stream('GET', path, headers=self._headers())
            async with request as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise DomainError('MODEL_TEMPORARILY_UNAVAILABLE', 503, retryable=True)
                if response.status_code == 404:
                    raise DomainError('MODEL_NOT_INSTALLED', 503)
                if response.status_code in {401, 403}:
                    raise DomainError('MODEL_REQUEST_REJECTED', 503)
                if response.status_code != 200:
                    raise DomainError('MODEL_REQUEST_REJECTED', 503)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise DomainError('MODEL_RESPONSE_TOO_LARGE', 503)
                return JSON_OBJECT.validate_json(bytes(body))

    async def _bounded_raw(self, base: str, path: str, payload: JSON_DICT | None, seconds: float) -> JSON_DICT:
        if self.cancel.is_set():
            raise DomainError('MODEL_CANCELLED', 409)

        async def cancelled() -> None:
            while not self.cancel.is_set():
                await asyncio.sleep(.05)

        request = asyncio.create_task(self._http_raw(base, path, payload, seconds))
        cancellation = asyncio.create_task(cancelled())
        try:
            done, _ = await asyncio.wait({request, cancellation}, timeout=seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
            if self.cancel.is_set():
                raise DomainError('MODEL_CANCELLED', 409)
            if request not in done:
                raise DomainError('MODEL_DEADLINE_EXCEEDED', 503, retryable=True)
            return await request
        finally:
            request.cancel()
            cancellation.cancel()
            await asyncio.gather(request, cancellation, return_exceptions=True)

    def _request_raw(self, base: str, path: str, payload: JSON_DICT | None = None) -> JSON_DICT:
        seconds = self.overall_seconds if self._deadline is None else self._deadline - time.monotonic()
        if payload is None:
            seconds = min(seconds, 5)
        if seconds <= 0:
            raise DomainError('MODEL_DEADLINE_EXCEEDED', 503, retryable=True)
        try:
            return asyncio.run(self._bounded_raw(base, path, payload, seconds))
        except httpx.TimeoutException:
            raise DomainError('MODEL_DEADLINE_EXCEEDED', 503, retryable=True) from None
        except httpx.TransportError:
            raise DomainError('MODEL_CONNECTION_FAILED', 503, retryable=True) from None
        except (ValidationError, ValueError):
            raise DomainError('MODEL_RESPONSE_INVALID', 503) from None

    def _model_names(self, base: str) -> list[str]:
        body = self._request_raw(base, '/v1/models')
        data = body.get('data')
        if not isinstance(data, list):
            raise DomainError('MODEL_RESPONSE_INVALID', 503)
        names = []
        for item in data:
            model_id = item.get('id') if isinstance(item, dict) else None
            if not isinstance(model_id, str) or not model_id.strip():
                raise DomainError('MODEL_RESPONSE_INVALID', 503)
            names.append(model_id)
        return names

    def status(self) -> dict[str, object]:
        try:
            embedding_models = self._model_names(self.embed_base)
            generation_models = self._model_names(self.base)
            models = list(dict.fromkeys([*embedding_models, *generation_models]))
            return {
                'online': True,
                'ready': self.embedding in embedding_models and self.generation in generation_models,
                'models': models,
                'provider': 'vllm',
                'embedding': self.embedding,
                'generation': self.generation,
                'embedding_digest': self.embedding_digest,
                'generation_digest': self.generation_digest,
                'embedding_url': self.embed_base,
                'generation_url': self.base,
                'digest_verification': 'served_model_id_only',
            }
        except DomainError as exc:
            return {
                'online': False,
                'ready': False,
                'models': [],
                'provider': 'vllm',
                'embedding': self.embedding,
                'generation': self.generation,
                'embedding_digest': self.embedding_digest,
                'generation_digest': self.generation_digest,
                'embedding_url': self.embed_base,
                'generation_url': self.base,
                'digest_verification': 'served_model_id_only',
                'message': str(exc),
            }

    def runtime_version(self) -> str:
        version = self._request_raw(self.base, '/version').get('version')
        if not isinstance(version, str) or not version.strip():
            raise DomainError('MODEL_RESPONSE_INVALID', 503)
        return version

    def identity(self) -> EmbeddingIdentity:
        if not HEX_DIGEST.fullmatch(self.embedding_digest):
            raise DomainError('MODEL_IDENTITY_UNAVAILABLE', 503)
        if self.embedding not in self._model_names(self.embed_base):
            raise DomainError('MODEL_NOT_INSTALLED', 503)
        return EmbeddingIdentity('vllm', self.embedding, self.embedding_digest)

    def model_key(self) -> str:
        identity = self.identity()
        return identity.tag + '@' + identity.digest

    def _map_request(self, path: str, payload: JSON_DICT | None) -> tuple[str, str, JSON_DICT | None]:
        if path == '/api/tags':
            return self.embed_base, '/v1/models', None
        if path == '/api/embed':
            inputs = payload.get('input') if isinstance(payload, dict) else None
            if not isinstance(inputs, list) or any(not isinstance(item, str) for item in inputs):
                raise DomainError('MODEL_REQUEST_REJECTED', 503)
            return self.embed_base, '/v1/embeddings', {
                'model': self.embedding,
                'input': inputs,
                'encoding_format': 'float',
            }
        if path == '/api/chat':
            if not isinstance(payload, dict) or not isinstance(payload.get('messages'), list):
                raise DomainError('MODEL_REQUEST_REJECTED', 503)
            raw_options = payload.get('options')
            options = raw_options if isinstance(raw_options, dict) else {}
            request: JSON_DICT = {
                'model': self.generation,
                'messages': payload['messages'],
                'stream': False,
                'temperature': options.get('temperature', 0),
                'max_tokens': options.get('num_predict', 3072),
                'seed': options.get('seed', 42),
            }
            schema = payload.get('format')
            if isinstance(schema, dict):
                # vLLM 0.10.2 + Qwen3 can loop on whitespace with the full
                # Pydantic schema. Keep syntax constrained by json_object and
                # enforce the exact contract locally with Pydantic below.
                request['response_format'] = {'type': 'json_object'}
                messages = payload.get('messages')
                if isinstance(messages, list) and messages and isinstance(messages[0], dict):
                    content = messages[0].get('content')
                    if isinstance(content, str):
                        messages[0]['content'] = content + _structured_json_hint(schema)
            elif schema == 'json':
                request['response_format'] = {'type': 'json_object'}
            return self.base, '/v1/chat/completions', request
        raise DomainError('MODEL_REQUEST_REJECTED', 503)

    def _digest_for(self, name: str) -> str:
        if name == self.embedding:
            return self.embedding_digest
        if name == self.generation:
            return self.generation_digest
        return ''

    def _map_response(self, path: str, body: JSON_DICT) -> JSON_DICT:
        if path == '/api/tags':
            data = body.get('data')
            if not isinstance(data, list):
                raise DomainError('MODEL_RESPONSE_INVALID', 503)
            models: list[JsonValue] = []
            for item in data:
                model_id = item.get('id') if isinstance(item, dict) else None
                if not isinstance(model_id, str):
                    raise DomainError('MODEL_RESPONSE_INVALID', 503)
                models.append({'name': model_id, 'digest': self._digest_for(model_id)})
            return {'models': models}
        if path == '/api/embed':
            data = body.get('data')
            if not isinstance(data, list):
                raise DomainError('MODEL_RESPONSE_INVALID', 503)
            vectors: list[list[JsonValue] | None] = [None] * len(data)
            for item in data:
                index_value = item.get('index') if isinstance(item, dict) else None
                if not isinstance(index_value, int) or isinstance(index_value, bool):
                    raise DomainError('MODEL_RESPONSE_INVALID', 503)
                index = index_value
                vector = item.get('embedding') if isinstance(item, dict) else None
                if not 0 <= index < len(data) or vectors[index] is not None or not isinstance(vector, list):
                    raise DomainError('MODEL_RESPONSE_INVALID', 503)
                vectors[index] = vector
            if any(vector is None for vector in vectors):
                raise DomainError('MODEL_RESPONSE_INVALID', 503)
            return {'embeddings': [vector for vector in vectors if vector is not None]}
        if path == '/api/chat':
            choices = body.get('choices')
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise DomainError('MODEL_RESPONSE_INVALID', 503)
            choice = choices[0]
            finish_reason = choice.get('finish_reason')
            if finish_reason not in {'stop', 'length'}:
                raise DomainError('MODEL_GENERATION_INVALID', 503)
            message = choice.get('message')
            if not isinstance(message, dict) or not isinstance(message.get('content'), str):
                raise DomainError('MODEL_RESPONSE_INVALID', 503)
            return {'done': True, 'done_reason': finish_reason,
                    'message': {'content': message['content']}}
        raise DomainError('MODEL_RESPONSE_INVALID', 503)

    def _request(self, path: str, payload: JSON_DICT | None = None) -> JSON_DICT:
        base, target, mapped = self._map_request(path, payload)
        return self._map_response(path, self._request_raw(base, target, mapped))

    def plan_queries(self, question: str, language: str) -> list[str]:
        try:
            return super().plan_queries(question, language)
        except DomainError as exc:
            raise ModelUnavailable('vLLM 检索问题翻译失败，使用原问题检索。') from exc


class VLLM(BoundedVLLM):
    """Legacy single-process facade with ModelUnavailable semantics."""

    def identity(self) -> EmbeddingIdentity:
        try:
            return super().identity()
        except DomainError as exc:
            raise ModelUnavailable('vLLM 模型身份不可用或嵌入模型未加载。') from exc

    def runtime_version(self) -> str:
        try:
            return super().runtime_version()
        except DomainError as exc:
            raise ModelUnavailable('vLLM 运行时版本不可用。') from exc

    def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            return super().embed(texts)
        except DomainError as exc:
            raise ModelUnavailable('vLLM 嵌入请求失败。') from exc

    def generate(self, question: str, evidence: list[dict[str, object]]) -> dict[str, object]:
        try:
            return super().generate(question, evidence)
        except DomainError as exc:
            raise ModelUnavailable('vLLM 生成请求失败。') from exc
