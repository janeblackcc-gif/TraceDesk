"""Run the bounded portfolio smoke harness and build an auditable evidence bundle."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Literal
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ingest import chunks, parse

from scripts import portfolio_report as _portfolio_report

CorpusFile = _portfolio_report.CorpusFile
CorpusManifest = _portfolio_report.CorpusManifest
PortfolioEnvironment = _portfolio_report.PortfolioEnvironment
PortfolioManifest = _portfolio_report.PortfolioManifest
PortfolioSample = _portfolio_report.PortfolioSample
sha256_file = _portfolio_report.sha256_file
validate_portfolio_run = _portfolio_report.validate_portfolio_run


DEFAULT_DOCUMENTS = 4
DEFAULT_CHUNKS_PER_DOCUMENT = 25
DEFAULT_SEED = 20260914
DEFAULT_CONCURRENCY = 5
DEFAULT_REQUESTS = 10
RunMode = Literal["dry-run", "real"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _write_json(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def _read_json_object(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path.name}")
    return value


def _write_jsonl(path: Path, rows: Iterable[dict[str, object]]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def _fixture_text(document_index: int, section_index: int, seed: int) -> str:
    # One heading plus one short paragraph yields exactly one parser chunk.
    payload = (
        f"Portfolio smoke fixture seed {seed}; document {document_index}; section {section_index}. "
        "This text is synthetic and exists only to exercise the bounded parser and index path."
    )
    return f"## Section {section_index:04d}\n{payload}\n\n"


def build_fixture_corpus(
    run_root: Path,
    *,
    documents: int = DEFAULT_DOCUMENTS,
    chunks_per_document: int = DEFAULT_CHUNKS_PER_DOCUMENT,
    seed: int = DEFAULT_SEED,
) -> CorpusManifest:
    """Create a deterministic, parser-verified synthetic corpus inside a run directory."""
    if not 1 <= documents <= 100:
        raise ValueError("documents must be between 1 and 100")
    if not 1 <= chunks_per_document <= 2500:
        raise ValueError("chunks_per_document must be between 1 and 2500")
    total_chunks = documents * chunks_per_document
    if total_chunks > 10000:
        raise ValueError("portfolio corpus is capped at 10000 chunks")

    corpus_dir = run_root / "corpus"
    corpus_dir.mkdir(parents=True, exist_ok=False)
    files: list[CorpusFile] = []
    for document_index in range(1, documents + 1):
        content = "".join(_fixture_text(document_index, section, seed)
                           for section in range(1, chunks_per_document + 1)).encode("utf-8")
        name = f"document-{document_index:03d}.md"
        path = corpus_dir / name
        with path.open("xb") as stream:
            stream.write(content)
        clean_name, pages = parse(name, content)
        parsed_chunks = chunks(pages)
        if clean_name != name or len(parsed_chunks) != chunks_per_document:
            raise RuntimeError(f"Fixture parser count mismatch for {name}")
        files.append(CorpusFile(path=f"corpus/{name}", sha256=sha256_file(path),
                                size_bytes=path.stat().st_size, chunk_count=len(parsed_chunks)))

    manifest = CorpusManifest(
        format_version=1,
        generated_at=_now(),
        materialization="generated",
        data_class="synthetic",
        seed=seed,
        documents=files,
        total_chunks=sum(item.chunk_count for item in files),
        total_bytes=sum(item.size_bytes for item in files),
    )
    _write_json(run_root / "corpus_manifest.json", json.loads(manifest.model_dump_json()))
    return manifest


def _fixture_environment() -> PortfolioEnvironment:
    def digest(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    return PortfolioEnvironment(
        captured_at=_now(),
        scope="portfolio-smoke",
        run_mode="dry-run",
        host="fixture",
        os="fixture-linux",
        kernel="fixture-kernel",
        cpu="fixture-cpu",
        ram_bytes=32 * 1024**3,
        gpu="fixture-gpu",
        vram_bytes=24 * 1024**3,
        gpu_driver="fixture-driver",
        docker_version="fixture-docker",
        compose_version="fixture-compose",
        python_version=platform.python_version(),
        image_digests={
            "app": "local-image-id:sha256:" + digest("app"),
            "parse-worker": "local-image-id:sha256:" + digest("parse-worker"),
            "parser": "local-image-id:sha256:" + digest("parser"),
            "proxy": "local-image-id:sha256:" + digest("proxy"),
        },
        model_provider="vllm",
        model_runtime_version="vllm-0.10.2-fixture",
        embedding_model="Qwen/Qwen3-Embedding-0.6B",
        embedding_digest="a" * 64,
        generation_model="Qwen/Qwen3-4B-Instruct-2507",
        generation_digest="b" * 64,
        data_class="synthetic",
        access_mode="restricted",
        resource_collection="fixture",
        tls_verification="verified",
        code_ref="dry-run-fixture",
        compose_config_sha256=digest("compose-fixture"),
    )


def _fixture_samples(start: datetime) -> list[PortfolioSample]:
    samples: list[PortfolioSample] = []
    offset = 0

    def add(**values: object) -> None:
        nonlocal offset
        values.setdefault("timestamp", start + timedelta(seconds=offset))
        values.setdefault("source", "fixture")
        samples.append(PortfolioSample.model_validate(values))
        offset += 1

    for target, latency in (("/livez", 8.0), ("/readyz", 22.0)):
        add(scenario="preflight", operation="health", target=target, ok=True, latency_ms=latency,
            status_code=200)
    for latency in (320.0, 280.0, 410.0, 360.0, 295.0, 335.0, 390.0, 305.0, 375.0, 345.0):
        add(scenario="steady_observation", operation="rag", target="synthetic-query", ok=True,
            latency_ms=latency, status_code=200)
    for index in range(20):
        add(scenario="steady_observation", operation="resource", target="application", ok=True,
            rss_bytes=480_000_000 + index * 100_000, vram_bytes=19_000_000_000 + index * 1_000_000,
            query_queue_depth=min(index // 5, 2))
    for latency in (18.0, 24.0, 21.0, 31.0, 27.0):
        add(scenario="steady_query", operation="api", target="query-submit", ok=True,
            latency_ms=latency, status_code=202)
    for scenario, error_code in (
        ("startup", "STARTUP_COMPLETE"),
        ("parse_index_query", "PIPELINE_COMPLETE"),
        ("model_restart", "MODEL_CONNECTION_FAILED"),
        ("worker_restart", "WORKER_RESTARTED"),
        ("database_restart", "DATABASE_UNAVAILABLE"),
    ):
        if scenario in {"model_restart", "worker_restart", "database_restart"}:
            add(scenario=scenario, operation="control", target="fault-injection", ok=False,
                error_code=error_code)
            add(scenario=scenario, operation="control", target="recovery", ok=True)
        else:
            add(scenario=scenario, operation="control", target="pipeline", ok=True)
    return samples


def _environment_from_json(path: Path) -> PortfolioEnvironment:
    if not path.is_file():
        raise ValueError("environment JSON is missing")
    return PortfolioEnvironment.model_validate_json(path.read_text(encoding="utf-8-sig"))


def _manifest(
    run_id: str,
    run_mode: RunMode,
    start: datetime,
    finish: datetime,
    environment: PortfolioEnvironment,
    corpus: CorpusManifest,
    samples_path: Path,
    run_root: Path,
    registered_users: int | None,
    query_concurrency: int | None,
    health_concurrency: int | None,
    scenarios: list[str],
) -> PortfolioManifest:
    environment_path = run_root / "environment.json"
    corpus_path = run_root / "corpus_manifest.json"
    return PortfolioManifest(
        format_version=1,
        run_id=run_id,
        scope="portfolio-smoke",
        run_mode=run_mode,
        coverage="harness-fixture" if run_mode == "dry-run" else "health-only",
        started_at=start,
        finished_at=finish,
        environment_path=environment_path.relative_to(run_root).as_posix(),
        environment_sha256=sha256_file(environment_path),
        samples_path=samples_path.relative_to(run_root).as_posix(),
        samples_sha256=sha256_file(samples_path),
        corpus_manifest_path=corpus_path.relative_to(run_root).as_posix(),
        corpus_manifest_sha256=sha256_file(corpus_path),
        chunk_count=corpus.total_chunks,
        registered_users=registered_users,
        query_concurrency=query_concurrency,
        health_concurrency=health_concurrency,
        scenarios=scenarios,
        data_class=environment.data_class,
        access_mode=environment.access_mode,
        resource_collection=environment.resource_collection,
        tls_verification=environment.tls_verification,
        formal_claim="none",
    )


def run_dry_run(
    output: Path,
    *,
    documents: int = DEFAULT_DOCUMENTS,
    chunks_per_document: int = DEFAULT_CHUNKS_PER_DOCUMENT,
    registered_users: int = 3,
    query_concurrency: int = 5,
    seed: int = DEFAULT_SEED,
) -> dict[str, object]:
    """Build and validate a non-evidence fixture bundle."""
    output.mkdir(parents=True, exist_ok=False)
    start = _now()
    corpus = build_fixture_corpus(output, documents=documents, chunks_per_document=chunks_per_document, seed=seed)
    environment = _fixture_environment()
    _write_json(output / "environment.json", json.loads(environment.model_dump_json()))
    samples = _fixture_samples(start)
    samples_path = output / "samples.jsonl"
    _write_jsonl(samples_path, [json.loads(item.model_dump_json()) for item in samples])
    # Fixture timestamps intentionally span a bounded synthetic window.
    finish = start + timedelta(seconds=120)
    manifest = _manifest("portfolio-dry-run", "dry-run", start, finish, environment, corpus, samples_path, output,
                         registered_users, query_concurrency, None, sorted({sample.scenario for sample in samples}))
    _write_json(output / "portfolio_manifest.json", json.loads(manifest.model_dump_json()))
    _write_json(output / "report.json", validate_portfolio_run(output, allow_fixture=True))
    (output / "README.md").write_text(
        "# Portfolio smoke dry-run\n\n"
        "This directory validates the harness format only. It is a synthetic fixture and is not runtime, "
        "capacity, quality, or target-release evidence.\n",
        encoding="utf-8",
        newline="\n",
    )
    return _read_json_object(output / "report.json")


def _validate_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("base URL must be an HTTP(S) URL without credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("base URL must not include a path, query or fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("non-loopback portfolio endpoints must use HTTPS")
    return value.rstrip("/")


def _probe_once(base_url: str, path: str, *, verify: bool, timeout: float, scenario: str) -> PortfolioSample:
    parsed = urlsplit(path)
    if (not path.startswith("/") or "\n" in path or "\r" in path or parsed.query or parsed.fragment or
            parsed.scheme or parsed.netloc):
        raise ValueError("probe paths must be absolute and single-line")
    started = time.perf_counter()
    try:
        response = httpx.get(base_url + path, verify=verify, timeout=timeout, follow_redirects=False, trust_env=False)
        elapsed = (time.perf_counter() - started) * 1000
        ok = response.status_code == 200
        return PortfolioSample(
            timestamp=_now(), scenario=scenario, operation="health", source="runtime", ok=ok,
            latency_ms=elapsed, status_code=response.status_code,
            error_code=None if ok else f"HTTP_STATUS_{response.status_code}", target=path,
        )
    except httpx.TimeoutException:
        return PortfolioSample(timestamp=_now(), scenario=scenario, operation="health", source="runtime", ok=False,
                               error_code="HTTP_TIMEOUT", target=path,
                               latency_ms=(time.perf_counter() - started) * 1000)
    except httpx.HTTPError:
        return PortfolioSample(timestamp=_now(), scenario=scenario, operation="health", source="runtime", ok=False,
                               error_code="HTTP_CLIENT_ERROR", target=path,
                               latency_ms=(time.perf_counter() - started) * 1000)


def run_real_health(
    output: Path,
    *,
    base_url: str,
    environment_path: Path,
    paths: list[str],
    concurrency: int = DEFAULT_CONCURRENCY,
    requests: int = DEFAULT_REQUESTS,
    timeout: float = 10.0,
    insecure: bool = False,
) -> dict[str, object]:
    """Run the P0 runtime health harness; authenticated workflows are a P1 task."""
    if not 1 <= concurrency <= 20 or not 1 <= requests <= 1000:
        raise ValueError("concurrency must be 1..20 and requests must be 1..1000")
    base_url = _validate_base_url(base_url)
    environment = _environment_from_json(environment_path)
    if environment.run_mode != "real":
        raise ValueError("real health mode requires an environment with run_mode=real")
    if environment.data_class != "synthetic":
        raise ValueError("P0 real health mode currently requires data_class=synthetic")
    environment = environment.model_copy(update={"tls_verification": "disabled" if insecure else "verified"})
    output.mkdir(parents=True, exist_ok=False)
    start = _now()
    corpus = build_fixture_corpus(output)
    # The P0 health harness never records credentials or response bodies.
    samples: list[PortfolioSample] = []
    for path in paths:
        samples.append(_probe_once(base_url, path, verify=not insecure, timeout=timeout, scenario="preflight"))
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [executor.submit(_probe_once, base_url, paths[0], verify=not insecure, timeout=timeout,
                                   scenario="steady_observation") for _ in range(requests)]
        for future in as_completed(futures):
            samples.append(future.result())
    finish = max(_now(), start + timedelta(seconds=1))
    # Use the captured environment file as an immutable input, copying only metadata.
    _write_json(output / "environment.json", json.loads(environment.model_dump_json()))
    samples_path = output / "samples.jsonl"
    _write_jsonl(samples_path, [json.loads(item.model_dump_json()) for item in samples])
    manifest = _manifest("portfolio-health", "real", start, finish, environment, corpus, samples_path, output,
                         None, None, concurrency, sorted({sample.scenario for sample in samples}))
    _write_json(output / "portfolio_manifest.json", json.loads(manifest.model_dump_json()))
    report = validate_portfolio_run(output)
    _write_json(output / "report.json", report)
    (output / "README.md").write_text(
        "# Portfolio smoke health run\n\n"
        "P0 health-only runtime probe. It records status and timing, never response bodies or credentials. "
        "Authenticated parse/index/query and host resource sampling belong to P1.\n",
        encoding="utf-8",
        newline="\n",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Build a synthetic harness fixture")
    mode.add_argument("--real-health", action="store_true", help="Run bounded runtime health probes")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--documents", type=int, default=DEFAULT_DOCUMENTS)
    parser.add_argument("--chunks-per-document", type=int, default=DEFAULT_CHUNKS_PER_DOCUMENT)
    parser.add_argument("--registered-users", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--base-url")
    parser.add_argument("--environment-json", type=Path)
    parser.add_argument("--path", action="append", dest="paths")
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--insecure", action="store_true", help="Disable TLS verification; record only as operator choice")
    args = parser.parse_args()
    try:
        if args.dry_run:
            report = run_dry_run(args.output, documents=args.documents, chunks_per_document=args.chunks_per_document,
                                 registered_users=args.registered_users, query_concurrency=args.concurrency, seed=args.seed)
        else:
            if not args.base_url or not args.environment_json:
                parser.error("--real-health requires --base-url and --environment-json")
            report = run_real_health(args.output, base_url=args.base_url, environment_path=args.environment_json,
                                     paths=args.paths or ["/livez", "/readyz"], concurrency=args.concurrency,
                                     requests=args.requests, timeout=args.timeout, insecure=args.insecure)
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_code": "PORTFOLIO_DRIVER_FAILED", "reason": str(exc)},
                         ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] in {"health-only", "fixture"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
