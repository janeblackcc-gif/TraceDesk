"""Validate bounded portfolio-smoke evidence without touching formal capacity gates."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


SHA256 = r"^[0-9a-f]{64}$"
IMAGE_REF = re.compile(r"^(?:\S+@sha256:[0-9a-f]{64}|local-image-id:sha256:[0-9a-f]{64})$")
REQUIRED_IMAGE_KEYS = {"app", "parse-worker", "parser", "proxy"}
FORBIDDEN_KEYS = {
    "password", "password_hash", "token", "session", "cookie", "secret", "private_key",
    "database_url", "question", "quote", "prompt", "authorization",
}
REQUIRED_SCENARIOS = {"preflight", "steady_observation"}


class PortfolioEvidenceError(ValueError):
    """Raised when a portfolio run cannot be treated as bounded evidence."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PortfolioEnvironment(StrictModel):
    captured_at: datetime
    scope: Literal["portfolio-smoke"]
    run_mode: Literal["dry-run", "real"]
    host: str = Field(min_length=1, max_length=200)
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
    model_provider: Literal["vllm"]
    model_runtime_version: str = Field(min_length=1, max_length=100)
    embedding_model: str = Field(min_length=1, max_length=200)
    embedding_digest: str = Field(pattern=SHA256)
    generation_model: str = Field(min_length=1, max_length=200)
    generation_digest: str = Field(pattern=SHA256)
    data_class: Literal["synthetic", "authorized-subset"]
    access_mode: Literal["restricted", "public"]
    resource_collection: Literal["not-collected", "fixture", "runtime"]
    tls_verification: Literal["verified", "disabled"]
    code_ref: str = Field(min_length=1, max_length=300)
    compose_config_sha256: str = Field(pattern=SHA256)

    @model_validator(mode="after")
    def coherent(self) -> "PortfolioEnvironment":
        if self.captured_at.utcoffset() is None:
            raise ValueError("Environment timestamp must include a timezone")
        if set(self.image_digests) != REQUIRED_IMAGE_KEYS:
            raise ValueError("image_digests must contain app, parse-worker, parser and proxy")
        if any(not IMAGE_REF.fullmatch(value) for value in self.image_digests.values()):
            raise ValueError("All image references must be pinned tag@sha256 or local-image-id:sha256")
        if self.run_mode == "real" and self.host == "fixture":
            raise ValueError("Real portfolio evidence cannot use the fixture host")
        if self.run_mode == "real":
            values = [self.host, self.os, self.kernel, self.cpu, self.gpu, self.gpu_driver,
                      self.docker_version, self.compose_version, self.model_runtime_version,
                      self.embedding_model, self.generation_model, self.code_ref]
            if any("REPLACE" in value or "fixture" in value.casefold() for value in values):
                raise ValueError("Real portfolio environment still contains fixture or REPLACE values")
            if any(".invalid" in value or "REPLACE" in value for value in self.image_digests.values()):
                raise ValueError("Real portfolio image references still contain placeholders")
            if self.embedding_digest == "0" * 64 or self.generation_digest == "0" * 64:
                raise ValueError("Real portfolio model digests must be captured values")
            if self.compose_config_sha256 == "0" * 64:
                raise ValueError("Real portfolio compose hash must be captured")
            if self.access_mode == "public" and self.tls_verification == "disabled":
                raise ValueError("Public portfolio runs require verified TLS")
        return self


class PortfolioSample(StrictModel):
    timestamp: datetime
    scenario: str = Field(min_length=1, max_length=100)
    operation: Literal["health", "api", "evidence", "queue", "rag", "resource", "control"]
    source: Literal["fixture", "runtime"]
    ok: bool
    latency_ms: float | None = Field(default=None, ge=0, le=600000)
    status_code: int | None = Field(default=None, ge=100, le=599)
    error_code: str | None = Field(default=None, max_length=100)
    rss_bytes: int | None = Field(default=None, ge=0)
    vram_bytes: int | None = Field(default=None, ge=0)
    query_queue_depth: int | None = Field(default=None, ge=0)
    target: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def coherent(self) -> "PortfolioSample":
        if self.timestamp.utcoffset() is None:
            raise ValueError("Sample timestamp must include a timezone")
        if self.operation in {"health", "api", "evidence", "queue", "rag"} and self.latency_ms is None:
            raise ValueError("Latency operations require latency_ms")
        if self.operation == "resource" and self.rss_bytes is None:
            raise ValueError("Resource samples require rss_bytes")
        if self.ok and self.error_code is not None:
            raise ValueError("Successful samples cannot carry an error code")
        if not self.ok and not self.error_code:
            raise ValueError("Failed samples require an error code")
        for value in (self.latency_ms,):
            if value is not None and not math.isfinite(value):
                raise ValueError("Portfolio values must be finite")
        return self


class PortfolioManifest(StrictModel):
    format_version: Literal[1]
    run_id: str = Field(min_length=1, max_length=200)
    scope: Literal["portfolio-smoke"]
    run_mode: Literal["dry-run", "real"]
    coverage: Literal["harness-fixture", "health-only"]
    started_at: datetime
    finished_at: datetime
    environment_path: str
    environment_sha256: str = Field(pattern=SHA256)
    samples_path: str
    samples_sha256: str = Field(pattern=SHA256)
    corpus_manifest_path: str
    corpus_manifest_sha256: str = Field(pattern=SHA256)
    chunk_count: int = Field(ge=1)
    active_chunks: int | None = Field(default=None, ge=1)
    registered_users: int | None = Field(default=None, ge=1)
    query_concurrency: int | None = Field(default=None, ge=1)
    health_concurrency: int | None = Field(default=None, ge=1)
    scenarios: list[str] = Field(min_length=1)
    data_class: Literal["synthetic", "authorized-subset"]
    access_mode: Literal["restricted", "public"]
    resource_collection: Literal["not-collected", "fixture", "runtime"]
    tls_verification: Literal["verified", "disabled"]
    formal_claim: Literal["none"]

    @model_validator(mode="after")
    def coherent(self) -> "PortfolioManifest":
        if self.started_at.utcoffset() is None or self.finished_at.utcoffset() is None:
            raise ValueError("Portfolio timestamps must include a timezone")
        if self.finished_at <= self.started_at:
            raise ValueError("Portfolio finish must be after start")
        for value in (self.environment_path, self.samples_path, self.corpus_manifest_path):
            path = PurePosixPath(value)
            if not value or "\\" in value or path.is_absolute() or ".." in path.parts:
                raise ValueError("Portfolio paths must be normalized relative paths")
        if len({self.environment_path, self.samples_path, self.corpus_manifest_path}) != 3:
            raise ValueError("Portfolio evidence paths must be distinct")
        if len(set(self.scenarios)) != len(self.scenarios):
            raise ValueError("Portfolio scenarios must be distinct")
        if self.run_mode == "dry-run":
            if self.coverage != "harness-fixture" or self.health_concurrency is not None:
                raise ValueError("Dry runs must use harness-fixture coverage")
        elif (self.coverage != "health-only" or self.active_chunks is not None or
              self.registered_users is not None or self.query_concurrency is not None or
              self.health_concurrency is None):
            raise ValueError("Health-only runs must not claim active chunks, registered users or RAG concurrency")
        return self


class CorpusFile(StrictModel):
    path: str
    sha256: str = Field(pattern=SHA256)
    size_bytes: int = Field(gt=0)
    chunk_count: int = Field(ge=1)

    @model_validator(mode="after")
    def safe_path(self) -> "CorpusFile":
        path = PurePosixPath(self.path)
        if not self.path or "\\" in self.path or path.is_absolute() or ".." in path.parts:
            raise ValueError("Corpus paths must be normalized relative paths")
        return self


class CorpusManifest(StrictModel):
    format_version: Literal[1]
    generated_at: datetime
    materialization: Literal["generated", "snapshot"]
    data_class: Literal["synthetic", "authorized-subset"]
    seed: int
    documents: list[CorpusFile] = Field(min_length=1)
    total_chunks: int = Field(ge=1)
    total_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def coherent(self) -> "CorpusManifest":
        if self.generated_at.utcoffset() is None:
            raise ValueError("Corpus timestamp must include a timezone")
        if self.total_chunks != sum(item.chunk_count for item in self.documents):
            raise ValueError("Corpus total_chunks does not match document counts")
        if self.total_bytes != sum(item.size_bytes for item in self.documents):
            raise ValueError("Corpus total_bytes does not match document sizes")
        paths = [item.path for item in self.documents]
        if len(set(paths)) != len(paths):
            raise ValueError("Corpus document paths must be distinct")
        return self


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _safe_file(root: Path, relative: str) -> Path:
    path = root / relative
    current = root
    for part in PurePosixPath(relative).parts:
        current /= part
        if current.is_symlink():
            raise PortfolioEvidenceError("Portfolio evidence cannot use symlinks")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise PortfolioEvidenceError(f"Portfolio evidence file is missing or outside the run: {relative}")
    return path


def _reject_forbidden_keys(value: object, location: str = "root") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                raise PortfolioEvidenceError(f"Forbidden sensitive field in portfolio evidence: {location}.{key}")
            _reject_forbidden_keys(item, f"{location}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_forbidden_keys(item, f"{location}[{index}]")


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Cannot compute a percentile without samples")
    rank = max(0, math.ceil(len(ordered) * quantile) - 1)
    return round(ordered[rank], 3)


def _load_samples(path: Path) -> list[PortfolioSample]:
    if path.stat().st_size > 64 * 1024 * 1024:
        raise PortfolioEvidenceError("Portfolio samples exceed the 64 MiB review limit")
    rows: list[PortfolioSample] = []
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                rows.append(PortfolioSample.model_validate_json(line))
            except Exception as exc:
                raise PortfolioEvidenceError(f"Invalid portfolio sample at line {number}") from exc
    if not rows:
        raise PortfolioEvidenceError("Portfolio samples cannot be empty")
    return rows


def _latency_summary(samples: list[PortfolioSample]) -> dict[str, dict[str, float | int]]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for sample in samples:
        if sample.ok and sample.latency_ms is not None and sample.operation not in {"control", "resource"}:
            grouped[sample.operation].append(sample.latency_ms)
    return {
        operation: {
            "samples": len(values),
            "p50_ms": _percentile(values, 0.50),
            "p95_ms": _percentile(values, 0.95),
        }
        for operation, values in sorted(grouped.items())
    }


def validate_portfolio_run(directory: Path, *, allow_fixture: bool = False) -> dict[str, object]:
    """Validate a portfolio run and return a bounded, non-formal summary.

    A dry-run is deliberately reported as ``fixture``.  It is useful for testing
    the harness but cannot be treated as runtime evidence.
    """
    directory = Path(directory)
    root = directory.resolve()
    if directory.is_symlink() or not root.is_dir():
        raise PortfolioEvidenceError("Portfolio run must be a real directory")

    manifest_path = _safe_file(root, "portfolio_manifest.json")
    manifest = PortfolioManifest.model_validate_json(manifest_path.read_text(encoding="utf-8-sig"))
    environment_path = _safe_file(root, manifest.environment_path)
    samples_path = _safe_file(root, manifest.samples_path)
    corpus_path = _safe_file(root, manifest.corpus_manifest_path)
    if sha256_file(environment_path) != manifest.environment_sha256:
        raise PortfolioEvidenceError("Portfolio environment hash mismatch")
    if sha256_file(samples_path) != manifest.samples_sha256:
        raise PortfolioEvidenceError("Portfolio samples hash mismatch")
    if sha256_file(corpus_path) != manifest.corpus_manifest_sha256:
        raise PortfolioEvidenceError("Portfolio corpus manifest hash mismatch")

    environment_text = environment_path.read_text(encoding="utf-8-sig")
    environment_payload = json.loads(environment_text)
    _reject_forbidden_keys(environment_payload)
    environment = PortfolioEnvironment.model_validate_json(environment_text)
    corpus = CorpusManifest.model_validate_json(corpus_path.read_text(encoding="utf-8-sig"))
    for item in corpus.documents:
        file_path = _safe_file(root, item.path)
        if file_path.stat().st_size != item.size_bytes or sha256_file(file_path) != item.sha256:
            raise PortfolioEvidenceError(f"Corpus file hash or size mismatch: {item.path}")
    samples = sorted(_load_samples(samples_path), key=lambda item: item.timestamp)

    if environment.scope != manifest.scope or environment.run_mode != manifest.run_mode:
        raise PortfolioEvidenceError("Environment and portfolio manifest scope/mode differ")
    if environment.data_class != manifest.data_class or corpus.data_class != manifest.data_class:
        raise PortfolioEvidenceError("Portfolio data class differs across evidence files")
    if environment.access_mode != manifest.access_mode:
        raise PortfolioEvidenceError("Portfolio access mode differs across evidence files")
    if environment.resource_collection != manifest.resource_collection:
        raise PortfolioEvidenceError("Portfolio resource collection differs across evidence files")
    if environment.tls_verification != manifest.tls_verification:
        raise PortfolioEvidenceError("Portfolio TLS verification mode differs across evidence files")
    if corpus.total_chunks != manifest.chunk_count:
        raise PortfolioEvidenceError("Portfolio chunk count differs from corpus manifest")
    if manifest.run_mode == "dry-run" and not allow_fixture:
        raise PortfolioEvidenceError("DRY_RUN_NOT_EVIDENCE: pass --allow-fixture for harness validation")

    start = manifest.started_at.astimezone(timezone.utc)
    finish = manifest.finished_at.astimezone(timezone.utc)
    for sample in samples:
        stamp = sample.timestamp.astimezone(timezone.utc)
        if not start <= stamp <= finish:
            raise PortfolioEvidenceError("Portfolio sample falls outside the declared run window")
        expected_source = "fixture" if manifest.run_mode == "dry-run" else "runtime"
        if sample.source != expected_source:
            raise PortfolioEvidenceError("Portfolio sample source does not match run mode")

    observed_scenarios = {item.scenario for item in samples}
    if set(manifest.scenarios) != observed_scenarios:
        raise PortfolioEvidenceError("Declared and observed portfolio scenarios differ")
    failures: list[str] = []
    for scenario in REQUIRED_SCENARIOS:
        scenario_samples = [item for item in samples if item.scenario == scenario]
        if not scenario_samples:
            failures.append(f"SCENARIO_MISSING:{scenario}")
        elif not any(item.ok for item in scenario_samples):
            failures.append(f"SCENARIO_NO_SUCCESS:{scenario}")
    if manifest.resource_collection in {"fixture", "runtime"} and not any(item.operation == "resource" for item in samples):
        failures.append("RESOURCE_SAMPLES_NOT_COLLECTED")
    if manifest.resource_collection == "runtime" and any(
        item.operation == "resource" and item.source != "runtime" for item in samples
    ):
        failures.append("RUNTIME_RESOURCE_SOURCE_INVALID")
    if manifest.run_mode == "real" and not any(item.source == "runtime" for item in samples):
        failures.append("RUNTIME_SAMPLES_NOT_COLLECTED")

    errors = [item for item in samples if not item.ok]
    steady = [item for item in samples if item.scenario == "steady_observation" and item.operation != "resource"]
    steady_error_rate = round(sum(not item.ok for item in steady) / len(steady), 4) if steady else None
    if any(not item.ok for item in samples if item.scenario == "preflight"):
        failures.append("PREFLIGHT_FAILURE_OBSERVED")
    if steady_error_rate is not None and steady_error_rate > 0.05:
        failures.append("STEADY_ERROR_RATE_EXCEEDED")
    resource = [item for item in samples if item.operation == "resource" and item.rss_bytes is not None]
    rss_growth: int | None = None
    if resource and manifest.run_mode == "real":
        window = max(1, len(resource) // 10)
        recent_rss = [item.rss_bytes for item in resource[-window:] if item.rss_bytes is not None]
        initial_rss = [item.rss_bytes for item in resource[:window] if item.rss_bytes is not None]
        rss_growth = max(0, int(statistics.median(recent_rss) - statistics.median(initial_rss)))
    status = "failed" if failures else ("fixture" if manifest.run_mode == "dry-run" else "health-only")
    is_fixture = manifest.run_mode == "dry-run"
    return {
        "status": status,
        "formal": False,
        "scope": manifest.scope,
        "run_id": manifest.run_id,
        "run_mode": manifest.run_mode,
        "coverage": manifest.coverage,
        "duration_seconds": round((finish - start).total_seconds(), 3),
        "generated_fixture_chunks": manifest.chunk_count,
        "active_chunks": manifest.active_chunks,
        "registered_users": None if is_fixture else manifest.registered_users,
        "query_concurrency": None if is_fixture else manifest.query_concurrency,
        "fixture_registered_users": manifest.registered_users if is_fixture else None,
        "fixture_query_concurrency": manifest.query_concurrency if is_fixture else None,
        "health_concurrency": manifest.health_concurrency,
        "data_class": manifest.data_class,
        "access_mode": manifest.access_mode,
        "resource_collection": manifest.resource_collection,
        "tls_verification": manifest.tls_verification,
        "scenario_count": len(observed_scenarios),
        "sample_count": len(samples),
        "error_sample_count": len(errors),
        "steady_error_rate": steady_error_rate,
        "latency": {} if is_fixture else _latency_summary(samples),
        "resource_samples": len(resource),
        "rss_growth_bytes": rss_growth,
        "scenario_counts": dict(sorted(Counter(item.scenario for item in samples).items())),
        "failures": sorted(set(failures)),
        "metrics_status": "fixture-only" if is_fixture else "runtime-health-only",
        "performance_gate": "not_applied",
        "formal_claim": "none",
        "notice": "Portfolio smoke is bounded engineering evidence; it is not target-release or formal capacity evidence.",
    }


def _write_new_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--allow-fixture", action="store_true", help="Report a dry-run as fixture instead of rejecting it")
    args = parser.parse_args()
    try:
        report = validate_portfolio_run(args.run, allow_fixture=args.allow_fixture)
    except Exception as exc:
        report = {
            "status": "failed",
            "formal": False,
            "scope": "portfolio-smoke",
            "error_code": "PORTFOLIO_EVIDENCE_INVALID",
            "reason": str(exc),
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        report["checked_at"] = datetime.now(timezone.utc).isoformat()
    try:
        _write_new_json(args.report, report)
    except FileExistsError:
        parser.error("Report already exists; select a new path")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"health-only", "fixture"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
