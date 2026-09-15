"""Select the configured local model provider without silent fallback."""
from __future__ import annotations

import threading

import httpx

from app.config import Settings
from app.providers import Ollama
from .ollama import BoundedOllama
from .vllm import BoundedVLLM, VLLM


def create_provider(settings: Settings | None = None) -> Ollama | VLLM:
    settings = settings or Settings.load()
    if settings.model_provider == 'vllm':
        return VLLM(settings)
    return Ollama(settings=settings)


def create_bounded_provider(settings: Settings, *, cancel: threading.Event | None = None,
                            overall_seconds: float = 120,
                            transport: httpx.AsyncBaseTransport | None = None) -> BoundedOllama | BoundedVLLM:
    if settings.model_provider == 'vllm':
        return BoundedVLLM(settings, cancel=cancel, overall_seconds=overall_seconds, transport=transport)
    return BoundedOllama(settings, cancel=cancel, overall_seconds=overall_seconds, transport=transport)
