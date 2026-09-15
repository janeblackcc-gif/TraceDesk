"""Reuse RC2 prompts, adding bounded local HTTP and strict model identity."""
from __future__ import annotations

import asyncio
import math
import re
import threading
import time
from contextlib import contextmanager

import httpx
from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from app.config import Settings, local_ollama_url
from app.providers import ModelUnavailable, Ollama
from app.services.errors import DomainError
from .profile import EmbeddingIdentity

JSON_OBJECT = TypeAdapter(dict[str, JsonValue])


class ModelTag(BaseModel):
    name: str
    digest: str


class ModelList(BaseModel):
    models: list[ModelTag]


class BoundedOllama(Ollama):
    def __init__(self, settings: Settings, *, cancel: threading.Event | None = None,
                 overall_seconds: float = 120, transport: httpx.AsyncBaseTransport | None = None):
        if overall_seconds <= 0:
            raise ValueError('overall_seconds must be positive')
        self.base = local_ollama_url(settings.ollama_url)
        self.embedding, self.generation = settings.embedding_model, settings.generation_model
        self.cancel = cancel or threading.Event()
        self.overall_seconds, self.transport = overall_seconds, transport
        self._deadline: float | None = None

    @contextmanager
    def operation(self):
        previous = self._deadline
        if previous is None:
            self._deadline = time.monotonic() + self.overall_seconds
        try:
            yield
        finally:
            self._deadline = previous

    async def _http(self, path: str, payload: dict | None) -> dict[str, JsonValue]:
        async with httpx.AsyncClient(base_url=self.base, timeout=httpx.Timeout(30, connect=3),
                transport=self.transport, trust_env=False, follow_redirects=False) as client:
            async with client.stream('GET' if payload is None else 'POST', path, json=payload) as response:
                if response.status_code == 429 or response.status_code >= 500:
                    raise DomainError('MODEL_TEMPORARILY_UNAVAILABLE', 503, retryable=True)
                if response.status_code == 404:
                    raise DomainError('MODEL_NOT_INSTALLED', 503)
                if response.status_code != 200:
                    raise DomainError('MODEL_REQUEST_REJECTED', 503)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 4 * 1024 * 1024:
                        raise DomainError('MODEL_RESPONSE_TOO_LARGE', 503)
                return JSON_OBJECT.validate_json(bytes(body))

    async def _bounded(self, path: str, payload: dict | None, seconds: float) -> dict[str, JsonValue]:
        if self.cancel.is_set():
            raise DomainError('MODEL_CANCELLED', 409)
        async def cancelled() -> None:
            while not self.cancel.is_set():
                await asyncio.sleep(.05)
        request = asyncio.create_task(self._http(path, payload))
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

    def _request(self, path: str, payload: dict | None = None) -> dict[str, JsonValue]:
        seconds = self.overall_seconds if self._deadline is None else self._deadline - time.monotonic()
        if payload is None:
            seconds = min(seconds, 5)
        if seconds <= 0:
            raise DomainError('MODEL_DEADLINE_EXCEEDED', 503, retryable=True)
        try:
            return asyncio.run(self._bounded(path, payload, seconds))
        except httpx.TimeoutException:
            raise DomainError('MODEL_DEADLINE_EXCEEDED', 503, retryable=True) from None
        except httpx.TransportError:
            raise DomainError('MODEL_CONNECTION_FAILED', 503, retryable=True) from None
        except (ValidationError, ValueError):
            raise DomainError('MODEL_RESPONSE_INVALID', 503) from None

    def identity(self) -> EmbeddingIdentity:
        try:
            models = ModelList.model_validate(self._request('/api/tags'))
        except ValidationError:
            raise DomainError('MODEL_IDENTITY_UNAVAILABLE', 503) from None
        for model in models.models:
            if model.name == self.embedding:
                digest = model.digest.removeprefix('sha256:')
                if not re.fullmatch('[0-9a-f]{64}', digest):
                    raise DomainError('MODEL_IDENTITY_UNAVAILABLE', 503)
                return EmbeddingIdentity('ollama', model.name, digest)
        raise DomainError('MODEL_NOT_INSTALLED', 503)

    def model_key(self) -> str:
        identity = self.identity()
        return identity.tag + '@' + identity.digest

    def embed(self, texts: list[str]) -> list[list[float]]:
        with self.operation():
            try:
                values = super().embed(texts)
            except ModelUnavailable:
                raise DomainError('MODEL_VECTOR_INVALID', 503) from None
            if any(len(vector) != 1024 or not 0 < sum(v * v for v in vector) < math.inf for vector in values):
                raise DomainError('MODEL_VECTOR_INVALID', 503)
            return values

    def generate(self, question: str, evidence: list[dict]) -> dict:
        with self.operation():
            try:
                return super().generate(question, evidence)
            except ModelUnavailable:
                raise DomainError('MODEL_GENERATION_INVALID', 503) from None
