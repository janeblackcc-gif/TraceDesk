"""Deterministic capacity evidence validation and SLO gating."""
from __future__ import annotations

import hashlib
import math
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHA256 = r'^[0-9a-f]{64}$'
PINNED_IMAGE = re.compile(r'^\S+@sha256:[0-9a-f]{64}$')
REQUIRED_SCENARIOS = {
    'steady_query', 'parse_with_query', 'reindex_with_query', 'model_restart', 'worker_crash',
    'database_restart', 'queue_full', 'disk_pressure', 'stale_write_race', 'steady_observation',
}
STEADY_SCENARIOS = {'steady_query', 'parse_with_query', 'reindex_with_query', 'steady_observation'}
LIMITS_MS = {'api': 500, 'evidence': 2000, 'queue': 2000, 'rag': 30000}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class CapacityEnvironment(StrictModel):
    captured_at: datetime
    scope: Literal['local-readiness', 'target-release']
    os: str = Field(min_length=1, max_length=200)
    kernel: str = Field(min_length=1, max_length=200)
    cpu: str = Field(min_length=1, max_length=300)
    ram_bytes: int = Field(gt=0)
    gpu: str = Field(min_length=1, max_length=300)
    vram_bytes: int = Field(gt=0)
    gpu_driver: str = Field(min_length=1, max_length=100)
    docker_version: str = Field(min_length=1, max_length=100)
    compose_version: str = Field(min_length=1, max_length=100)
    python_version: str = Field(min_length=1, max_length=100)
    image_digests: dict[str, str]
    postgres_version: str = Field(min_length=1, max_length=100)
    pgvector_version: str = Field(min_length=1, max_length=100)
    model_provider: Literal['ollama', 'vllm'] | None = None
    model_runtime_version: str | None = Field(default=None, min_length=1, max_length=100)
    ollama_version: str | None = Field(default=None, min_length=1, max_length=100)
    embedding_model: str = Field(min_length=1, max_length=200)
    embedding_digest: str = Field(pattern=SHA256)
    generation_model: str = Field(min_length=1, max_length=200)
    generation_digest: str = Field(pattern=SHA256)
    disk_total_bytes: int = Field(gt=0)
    timezone: str = Field(min_length=1, max_length=100)
    code_ref: str = Field(min_length=1, max_length=300)
    config_sha256: str = Field(pattern=SHA256)

    @model_validator(mode='after')
    def coherent(self) -> CapacityEnvironment:
        if self.captured_at.utcoffset() is None:
            raise ValueError('Environment timestamp must include a timezone')
        required = {'app', 'parse-worker', 'parser', 'proxy'}
        local_image = re.compile(r'^local-image-id:sha256:[0-9a-f]{64}$')
        valid_image = PINNED_IMAGE if self.scope == 'target-release' else re.compile(
            rf'(?:{PINNED_IMAGE.pattern})|(?:{local_image.pattern})')
        if set(self.image_digests) != required or any(not valid_image.fullmatch(value) for value in self.image_digests.values()):
            raise ValueError('All release images must use tag@sha256 references')
        if self.scope == 'target-release':
            if self.model_provider != 'vllm' or self.model_runtime_version is None or self.ollama_version is not None:
                raise ValueError('Target releases require an explicit vLLM runtime identity')
        elif self.model_provider == 'vllm':
            if self.model_runtime_version is None or self.ollama_version is not None:
                raise ValueError('vLLM evidence requires model_runtime_version only')
        elif self.ollama_version is None:
            raise ValueError('Legacy Ollama evidence requires ollama_version')
        return self


class CapacityManifest(StrictModel):
    format_version: Literal[1]
    run_id: str = Field(min_length=1, max_length=200)
    started_at: datetime
    finished_at: datetime
    environment_path: str
    environment_sha256: str = Field(pattern=SHA256)
    samples_path: str
    samples_sha256: str = Field(pattern=SHA256)
    active_chunks: int = Field(ge=1)
    registered_users: int = Field(ge=1)
    query_concurrency: int = Field(ge=1)
    max_error_rate: float = Field(ge=0, le=.05)
    max_rss_growth_bytes: int = Field(ge=0)
    max_vram_growth_bytes: int = Field(ge=0)
    scenarios: list[str] = Field(min_length=1)
    oom_events: int = Field(ge=0)
    data_corruption_events: int = Field(ge=0)
    scope_leaks: int = Field(ge=0)

    @model_validator(mode='after')
    def coherent(self) -> CapacityManifest:
        if self.started_at.utcoffset() is None or self.finished_at.utcoffset() is None:
            raise ValueError('Capacity timestamps must include a timezone')
        if self.finished_at <= self.started_at:
            raise ValueError('Capacity finish must be after start')
        for value in (self.environment_path, self.samples_path):
            path = PurePosixPath(value)
            if not value or '\\' in value or path.is_absolute() or '..' in path.parts:
                raise ValueError('Capacity files must use normalized relative paths')
        if self.environment_path == self.samples_path or len(set(self.scenarios)) != len(self.scenarios):
            raise ValueError('Capacity evidence files and scenarios must be distinct')
        return self


class CapacitySample(StrictModel):
    timestamp: datetime
    scenario: str = Field(min_length=1, max_length=100)
    operation: Literal['api', 'evidence', 'queue', 'rag', 'resource', 'control']
    ok: bool
    latency_ms: float | None = Field(default=None, ge=0, le=600000)
    error_code: str | None = Field(default=None, max_length=100)
    rss_bytes: int | None = Field(default=None, ge=0)
    vram_bytes: int | None = Field(default=None, ge=0)
    query_queue_depth: int | None = Field(default=None, ge=0)

    @model_validator(mode='after')
    def coherent(self) -> CapacitySample:
        if self.timestamp.utcoffset() is None:
            raise ValueError('Sample timestamp must include a timezone')
        if self.operation in LIMITS_MS and self.latency_ms is None:
            raise ValueError('Latency operations require latency_ms')
        if self.operation == 'resource' and self.rss_bytes is None:
            raise ValueError('Resource samples require rss_bytes')
        if self.ok and self.error_code is not None:
            raise ValueError('Successful samples cannot carry an error code')
        if not self.ok and not self.error_code:
            raise ValueError('Failed samples require an error code')
        for value in (self.latency_ms,):
            if value is not None and not math.isfinite(value):
                raise ValueError('Capacity values must be finite')
        return self


def percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError('Cannot compute a percentile without samples')
    rank = max(0, math.ceil(len(ordered) * quantile) - 1)
    return ordered[rank]


def _file(root: Path, relative: str) -> Path:
    path = root / relative
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        if current.is_symlink():
            raise ValueError('Capacity evidence cannot use symlinks')
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Capacity evidence file is missing or outside the run directory')
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _samples(path: Path) -> list[CapacitySample]:
    if path.stat().st_size > 256 * 1024 * 1024:
        raise ValueError('Capacity samples exceed the 256 MiB review limit')
    rows = []
    with path.open(encoding='utf-8-sig') as stream:
        for number, line in enumerate(stream, start=1):
            if line.strip():
                try:
                    rows.append(CapacitySample.model_validate_json(line))
                except Exception as exc:
                    raise ValueError(f'Invalid capacity sample at line {number}') from exc
    if not rows:
        raise ValueError('Capacity samples cannot be empty')
    return rows


def evaluate_capacity(directory: Path, *, formal: bool = False) -> dict[str, object]:
    root = directory.resolve()
    if directory.is_symlink() or not root.is_dir():
        raise ValueError('Capacity run must be a real directory')
    manifest_path = _file(root, 'capacity_manifest.json')
    manifest = CapacityManifest.model_validate_json(manifest_path.read_text(encoding='utf-8-sig'))
    environment_path = _file(root, manifest.environment_path)
    samples_path = _file(root, manifest.samples_path)
    if _sha256(environment_path) != manifest.environment_sha256 or _sha256(samples_path) != manifest.samples_sha256:
        raise ValueError('Capacity evidence hash mismatch')
    environment = CapacityEnvironment.model_validate_json(environment_path.read_text(encoding='utf-8-sig'))
    samples = sorted(_samples(samples_path), key=lambda item: item.timestamp)
    start = manifest.started_at.astimezone(timezone.utc)
    finish = manifest.finished_at.astimezone(timezone.utc)
    if any(not start <= item.timestamp.astimezone(timezone.utc) <= finish for item in samples):
        raise ValueError('Capacity sample falls outside the declared run window')
    observed_scenarios = {item.scenario for item in samples}
    if set(manifest.scenarios) != observed_scenarios:
        raise ValueError('Declared and observed capacity scenarios differ')
    failures = []
    duration_seconds = (finish - start).total_seconds()
    if formal:
        if environment.scope != 'target-release':
            failures.append('TARGET_ENVIRONMENT_REQUIRED')
        if duration_seconds < 1800:
            failures.append('DURATION_BELOW_30_MINUTES')
        if manifest.active_chunks < 50000:
            failures.append('ACTIVE_CHUNKS_BELOW_50000')
        if manifest.registered_users < 20:
            failures.append('USERS_BELOW_20')
        if manifest.query_concurrency < 5:
            failures.append('QUERY_CONCURRENCY_BELOW_5')
        if not REQUIRED_SCENARIOS <= set(manifest.scenarios):
            failures.append('REQUIRED_SCENARIOS_MISSING')
        if samples[0].timestamp.astimezone(timezone.utc) > start.replace(microsecond=0) + timedelta(seconds=60):
            failures.append('RUN_START_NOT_SAMPLED')
        if samples[-1].timestamp.astimezone(timezone.utc) < finish.replace(microsecond=0) - timedelta(seconds=60):
            failures.append('RUN_END_NOT_SAMPLED')
    steady = [item for item in samples if item.scenario in STEADY_SCENARIOS and item.operation != 'resource']
    steady_error_rate = sum(not item.ok for item in steady) / len(steady) if steady else 1.0
    if steady_error_rate > manifest.max_error_rate:
        failures.append('STEADY_ERROR_RATE_EXCEEDED')
    latency_summary: dict[str, dict[str, float | int]] = {}
    for operation, limit in LIMITS_MS.items():
        values = [item.latency_ms for item in samples if item.scenario in STEADY_SCENARIOS and
                  item.operation == operation and item.ok and item.latency_ms is not None]
        if formal and len(values) < 50:
            failures.append(f'{operation.upper()}_SAMPLES_BELOW_50')
        if values:
            summary = {'samples': len(values), 'p50_ms': percentile(values, .5),
                       'p95_ms': percentile(values, .95), 'p99_ms': percentile(values, .99), 'limit_p95_ms': limit}
            latency_summary[operation] = summary
            if summary['p95_ms'] > limit:
                failures.append(f'{operation.upper()}_P95_EXCEEDED')
    resource = [item for item in samples if item.operation == 'resource' and item.rss_bytes is not None]
    rss_growth = vram_growth = 0
    if formal and len(resource) < 20:
        failures.append('RESOURCE_SAMPLES_BELOW_20')
    if resource:
        window = max(1, len(resource) // 10)
        rss_values = [cast(int, item.rss_bytes) for item in resource]
        rss_growth = max(0, int(statistics.median(rss_values[-window:]) -
                                statistics.median(rss_values[:window])))
        vram = [item for item in resource if item.vram_bytes is not None]
        if vram:
            vwindow = max(1, len(vram) // 10)
            vram_values = [cast(int, item.vram_bytes) for item in vram]
            vram_growth = max(0, int(statistics.median(vram_values[-vwindow:]) -
                                     statistics.median(vram_values[:vwindow])))
    if rss_growth > manifest.max_rss_growth_bytes:
        failures.append('RSS_GROWTH_EXCEEDED')
    if vram_growth > manifest.max_vram_growth_bytes:
        failures.append('VRAM_GROWTH_EXCEEDED')
    max_queue = max((item.query_queue_depth or 0 for item in samples), default=0)
    if max_queue > 20:
        failures.append('QUERY_QUEUE_UNBOUNDED')
    queue_full = [item for item in samples if item.scenario == 'queue_full']
    if formal and not any(not item.ok and item.error_code == 'QUEUE_FULL' for item in queue_full):
        failures.append('QUEUE_FULL_REJECTION_NOT_OBSERVED')
    for scenario in ('model_restart', 'database_restart'):
        sequence = [item for item in samples if item.scenario == scenario and item.operation != 'resource']
        first_failure = next((index for index, item in enumerate(sequence) if not item.ok), None)
        recovered = first_failure is not None and any(item.ok for item in sequence[first_failure + 1:])
        if formal and not recovered:
            failures.append(f'{scenario.upper()}_FAILURE_RECOVERY_NOT_OBSERVED')
    if manifest.oom_events:
        failures.append('OOM_OBSERVED')
    if manifest.data_corruption_events:
        failures.append('DATA_CORRUPTION_OBSERVED')
    if manifest.scope_leaks:
        failures.append('SCOPE_LEAK_OBSERVED')
    return {
        'status': 'failed' if failures else 'passed',
        'formal': formal,
        'run_id': manifest.run_id,
        'duration_seconds': duration_seconds,
        'active_chunks': manifest.active_chunks,
        'registered_users': manifest.registered_users,
        'query_concurrency': manifest.query_concurrency,
        'scenario_count': len(observed_scenarios),
        'sample_count': len(samples),
        'steady_error_rate': steady_error_rate,
        'latency': latency_summary,
        'max_query_queue_depth': max_queue,
        'rss_growth_bytes': rss_growth,
        'vram_growth_bytes': vram_growth,
        'failures': sorted(set(failures)),
    }
