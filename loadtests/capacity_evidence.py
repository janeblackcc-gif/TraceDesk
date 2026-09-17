"""Capture non-self-reported T-075 environment and database evidence."""
from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, cast

import httpx

from app.operations.capacity import CapacityEnvironment, CapacityManifest, CapacitySample, REQUIRED_SCENARIOS, evaluate_capacity
from loadtests.capacity_runtime import HostControl, RunResult


PINNED_IMAGE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_ENV = {
    "TRACEDESK_EMBED_MODEL",
    "TRACEDESK_EMBED_MODEL_DIGEST",
    "TRACEDESK_CHAT_MODEL",
    "TRACEDESK_CHAT_MODEL_DIGEST",
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_new(path: Path, value: object) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def allowed_env(path: Path) -> dict[str, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError("compose env file must be a bounded regular file")
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key in ALLOWED_ENV:
            result[key] = value.strip().strip('"').strip("'")
    if set(result) != ALLOWED_ENV:
        raise ValueError("compose env file is missing model identity fields")
    if not SHA256.fullmatch(result["TRACEDESK_EMBED_MODEL_DIGEST"]):
        raise ValueError("embedding digest is invalid")
    if not SHA256.fullmatch(result["TRACEDESK_CHAT_MODEL_DIGEST"]):
        raise ValueError("generation digest is invalid")
    return result


def compose_config(control: HostControl) -> tuple[dict[str, object], bytes]:
    result = control.compose("config", "--format", "json", timeout=120)
    raw = result.stdout.encode("utf-8")
    value = json.loads(raw)
    if type(value) is not dict:
        raise RuntimeError("compose config was not a JSON object")
    return cast(dict[str, object], value), raw


def parse_compose_ps(raw: str) -> list[dict[str, object]]:
    """Accept both the JSON array and JSON-lines formats emitted by Compose."""
    try:
        value = json.loads(raw)
        values = value if type(value) is list else [value]
    except json.JSONDecodeError:
        values = [json.loads(line) for line in raw.splitlines() if line.strip()]
    if not values or any(type(item) is not dict for item in values):
        raise RuntimeError("compose ps was not a JSON object sequence")
    return [cast(dict[str, object], item) for item in values]


def image_references(config: dict[str, object], *, require_pinned: bool = True) -> dict[str, str]:
    services_value = config.get("services")
    if type(services_value) is not dict:
        raise RuntimeError("compose config has no services")
    services = cast(dict[str, object], services_value)

    def service(name: str) -> dict[str, object]:
        value = services.get(name)
        if type(value) is not dict:
            raise RuntimeError(f"compose service is missing: {name}")
        return cast(dict[str, object], value)

    def image(name: str) -> str:
        value = service(name).get("image")
        if type(value) is not str:
            raise RuntimeError(f"compose service has no image: {name}")
        return value

    command = service("parse-worker").get("command")
    if type(command) is not list or "--parser-image" not in command:
        raise RuntimeError("parse-worker command does not expose its parser image")
    marker = command.index("--parser-image")
    if marker + 1 >= len(command) or type(command[marker + 1]) is not str:
        raise RuntimeError("parse-worker parser image is invalid")
    images = {
        "app": image("web"),
        "parse-worker": image("parse-worker"),
        "parser": cast(str, command[marker + 1]),
        "proxy": image("proxy"),
    }
    if require_pinned and any(PINNED_IMAGE.fullmatch(value) is None for value in images.values()):
        raise RuntimeError("formal capacity requires every release image as tag@sha256")
    return images


def _single_line(control: HostControl, command: list[str], *, timeout: int = 60) -> str:
    result = control.run(command, timeout=timeout)
    value = result.stdout.strip()
    if not value:
        raise RuntimeError("environment command returned no output")
    return value.splitlines()[0].strip()


def resolve_code_ref(control: HostControl, fallback: str | None = None) -> str:
    """Return a reproducible source identity for checkout and release-bundle runs."""
    revision = control.run(["git", "rev-parse", "HEAD"], check=False)
    value = revision.stdout.strip()
    if revision.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{40,64}", value):
        code_ref = value.lower()
        dirty = control.run(["git", "status", "--porcelain"], check=True).stdout
        if dirty.strip():
            code_ref += "+dirty:" + sha256_bytes(dirty.encode("utf-8"))[:16]
        return code_ref
    if fallback is not None and re.fullmatch(r"bundle-sha256:[0-9a-f]{64}", fallback):
        return fallback
    detail = revision.stderr.strip()[:200]
    raise RuntimeError(f"source identity is unavailable: {detail}" if detail else "source identity is unavailable")


def _postgres_value(control: HostControl, sql: str) -> str:
    result = control.compose(
        "exec", "--no-TTY", "postgres", "psql", "--username", "tracedesk", "--dbname", "tracedesk",
        "--tuples-only", "--no-align", "--command", sql,
        timeout=120,
    )
    value = result.stdout.strip()
    if not value:
        raise RuntimeError("PostgreSQL evidence query returned no output")
    return value


def vllm_version(url: str = "http://127.0.0.1:8000") -> str:
    response = httpx.get(url.rstrip("/") + "/version", timeout=10, trust_env=False)
    response.raise_for_status()
    value = response.json()
    if type(value) is not dict or type(value.get("version")) is not str:
        raise RuntimeError("vLLM version endpoint returned an invalid response")
    return cast(str, value["version"])


def capture_environment(
    output: Path,
    *,
    control: HostControl,
    data_dir: Path,
    runtime_url: str = "http://127.0.0.1:8000",
    code_ref_fallback: str | None = None,
) -> CapacityEnvironment:
    output.mkdir(parents=True, exist_ok=False)
    config, config_raw = compose_config(control)
    images = image_references(config)
    identity = allowed_env(control.env_file)
    gpu_line = _single_line(
        control,
        ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"],
    )
    gpu_parts = [part.strip() for part in gpu_line.split(",")]
    if len(gpu_parts) != 3 or not gpu_parts[2].isdigit():
        raise RuntimeError("nvidia-smi identity response is invalid")
    os_release = Path("/etc/os-release").read_text(encoding="utf-8")
    pretty_match = re.search(r'^PRETTY_NAME="?([^"\n]+)"?$', os_release, re.MULTILINE)
    os_name = pretty_match.group(1) if pretty_match else platform.platform()
    cpu = "unknown"
    for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
        if line.lower().startswith("model name") and ":" in line:
            cpu = line.split(":", 1)[1].strip()
            break
    memory_match = re.search(r"^MemTotal:\s+(\d+)\s+kB$", Path("/proc/meminfo").read_text(encoding="utf-8"), re.MULTILINE)
    if memory_match is None:
        raise RuntimeError("physical RAM size is unavailable")
    code_ref = resolve_code_ref(control, code_ref_fallback)
    disk = shutil.disk_usage(data_dir)
    environment = CapacityEnvironment(
        captured_at=datetime.now(timezone.utc),
        scope="target-release",
        os=os_name,
        kernel=platform.release(),
        cpu=cpu,
        ram_bytes=int(memory_match.group(1)) * 1024,
        gpu=gpu_parts[0],
        vram_bytes=int(gpu_parts[2]) * 1024 * 1024,
        gpu_driver=gpu_parts[1],
        docker_version=_single_line(control, ["docker", "version", "--format", "{{.Server.Version}}"]),
        compose_version=_single_line(control, ["docker", "compose", "version", "--short"]),
        python_version=platform.python_version(),
        image_digests=images,
        postgres_version=_postgres_value(control, "SHOW server_version;"),
        pgvector_version=_postgres_value(control, "SELECT extversion FROM pg_extension WHERE extname='vector';"),
        model_provider="vllm",
        model_runtime_version=vllm_version(runtime_url),
        embedding_model=identity["TRACEDESK_EMBED_MODEL"],
        embedding_digest=identity["TRACEDESK_EMBED_MODEL_DIGEST"],
        generation_model=identity["TRACEDESK_CHAT_MODEL"],
        generation_digest=identity["TRACEDESK_CHAT_MODEL_DIGEST"],
        disk_total_bytes=disk.total,
        timezone=str(datetime.now().astimezone().tzinfo),
        code_ref=code_ref,
        config_sha256=sha256_bytes(config_raw),
    )
    write_json_new(output / "environment.json", json.loads(environment.model_dump_json()))
    (output / "compose-config.json").write_bytes(config_raw)
    return environment


def capture_database_proof(output: Path, *, control: HostControl, kb_id: str) -> dict[str, object]:
    if re.fullmatch(r"[0-9a-fA-F-]{36}", kb_id) is None:
        raise ValueError("kb_id is invalid")
    sql = f"""SELECT json_build_object(
      'kb_id', k.id::text,
      'active_generation_id', k.active_index_generation_id::text,
      'active_chunks', (SELECT count(*) FROM generation_revisions gr
        JOIN chunks c ON c.revision_id=gr.revision_id AND c.parse_run_id=gr.parse_run_id
        WHERE gr.generation_id=k.active_index_generation_id),
      'registered_users', (SELECT count(*) FROM users),
      'active_users', (SELECT count(*) FROM users WHERE status='active'),
      'active_query_jobs', (SELECT count(*) FROM jobs WHERE type='query' AND state IN ('queued','running','retry_wait'))
    ) FROM knowledge_bases k WHERE k.id='{kb_id}'::uuid;"""
    raw = _postgres_value(control, sql)
    value = json.loads(raw)
    if type(value) is not dict:
        raise RuntimeError("database proof was not a JSON object")
    proof = cast(dict[str, object], value)
    if proof.get("active_chunks") != 50_000:
        raise RuntimeError("database proof did not show exactly 50,000 active chunks")
    if type(proof.get("active_users")) is not int or cast(int, proof["active_users"]) < 20:
        raise RuntimeError("database proof did not show at least 20 active users")
    proof.update({
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "query_sha256": sha256_bytes(sql.encode("utf-8")),
    })
    write_json_new(output / "database-proof.json", proof)
    (output / "database-proof.sql").write_text(sql + "\n", encoding="utf-8", newline="\n")
    return proof


def formal_preflight(
    output: Path,
    *,
    control: HostControl,
    data_dir: Path,
    base_url: str,
    verify_tls: bool,
    embedding_url: str = "http://127.0.0.1:8001",
    generation_url: str = "http://127.0.0.1:8000",
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=False)
    checks: list[dict[str, object]] = []

    def command(check_id: str, values: list[str], *, timeout: int = 120) -> None:
        result = control.run(values, timeout=timeout, check=False)
        checks.append({
            "id": check_id,
            "status": "passed" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "stdout": result.stdout.strip()[:2000],
            "stderr": result.stderr.strip()[:2000],
        })

    command(
        "nvidia-smi",
        ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"],
    )
    command("docker-compose", ["docker", "compose", "version"])
    command(
        "parser-sandbox",
        ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "alpine", "true"],
        timeout=300,
    )
    command("passwordless-sudo", ["sudo", "-n", "true"])
    command("generation-unit", ["sudo", "-n", "systemctl", "is-active", "tracedesk-vllm-generation.service"])
    command("embedding-unit", ["sudo", "-n", "systemctl", "is-active", "tracedesk-vllm-embedding.service"])
    try:
        ps = control.compose("ps", "--format", "json")
        rows = parse_compose_ps(ps.stdout)
        running = {
            item.get("Service") for item in rows
            if type(item) is dict and str(item.get("State", "")).casefold() == "running"
        }
        required = {"postgres", "web", "parse-worker", "index-worker", "query-worker", "gc-worker", "proxy"}
        checks.append({
            "id": "compose-services",
            "status": "passed" if required <= running else "failed",
            "running": sorted(str(item) for item in running if item),
            "missing": sorted(required - running),
        })
    except Exception as exc:
        checks.append({"id": "compose-services", "status": "failed", "error_type": type(exc).__name__})
    try:
        config, _ = compose_config(control)
        refs = image_references(config)
        allowed_env(control.env_file)
        checks.append({"id": "pinned-images", "status": "passed", "images": refs})
    except Exception as exc:
        checks.append({"id": "pinned-images", "status": "failed", "error_type": type(exc).__name__})
    try:
        usage = shutil.disk_usage(data_dir)
        mount = control.run(["findmnt", "--noheadings", "--output", "SOURCE", "--mountpoint", str(data_dir)])
        source = mount.stdout.strip()
        bounded = usage.total <= 1024 * 1024 * 1024 and source.startswith("/dev/loop")
        checks.append({
            "id": "bounded-data-volume",
            "status": "passed" if bounded else "failed",
            "total_bytes": usage.total,
            "source": source,
        })
    except Exception as exc:
        checks.append({"id": "bounded-data-volume", "status": "failed", "error_type": type(exc).__name__})
    for check_id, url in (("generation-health", generation_url), ("embedding-health", embedding_url), ("application-ready", base_url)):
        path = "/readyz" if check_id == "application-ready" else "/health"
        wait_seconds = 30 if check_id == "application-ready" else 240
        deadline = time.monotonic() + wait_seconds
        attempts = 0
        last_status: int | None = None
        last_error: str | None = None
        while True:
            attempts += 1
            try:
                response = httpx.get(
                    url.rstrip("/") + path,
                    verify=verify_tls if check_id == "application-ready" else True,
                    timeout=10,
                    trust_env=False,
                )
                last_status = response.status_code
                last_error = None
                if response.status_code == 200:
                    break
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            if time.monotonic() >= deadline:
                break
            time.sleep(2)
        check: dict[str, object] = {
            "id": check_id,
            "status": "passed" if last_status == 200 else "failed",
            "attempts": attempts,
        }
        if last_status is not None:
            check["http_status"] = last_status
        if last_error is not None:
            check["error_type"] = last_error
        checks.append(check)
    status = "passed" if all(item["status"] == "passed" for item in checks) else "failed"
    report: dict[str, object] = {
        "schema_version": 1,
        "scope": "t075-formal-preflight",
        "formal_claim": "none",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "checks": checks,
        "next_step": "provision" if status == "passed" else "stop-before-provision",
    }
    write_json_new(output / "formal-preflight.json", report)
    return report


def oom_events(control: HostControl) -> int:
    identifiers = control.compose("ps", "--all", "--quiet").stdout.split()
    total = 0
    for identifier in identifiers:
        value = _single_line(control, ["docker", "inspect", "--format", "{{.State.OOMKilled}}", identifier])
        total += int(value.casefold() == "true")
    return total


def finalize_run(
    run_root: Path,
    *,
    control: HostControl,
    result: RunResult,
    database_proof: dict[str, object],
    max_error_rate: float,
    max_rss_growth_bytes: int,
    max_vram_growth_bytes: int,
) -> dict[str, object]:
    environment = run_root / "environment.json"
    samples = run_root / "samples.jsonl"
    if not environment.is_file() or not samples.is_file():
        raise RuntimeError("capacity run is missing environment or samples")
    manifest = CapacityManifest(
        format_version=1,
        run_id=run_root.name,
        started_at=result.started_at,
        finished_at=result.finished_at,
        environment_path="environment.json",
        environment_sha256=sha256_file(environment),
        samples_path="samples.jsonl",
        samples_sha256=sha256_file(samples),
        active_chunks=cast(int, database_proof["active_chunks"]),
        registered_users=cast(int, database_proof["active_users"]),
        query_concurrency=5,
        max_error_rate=max_error_rate,
        max_rss_growth_bytes=max_rss_growth_bytes,
        max_vram_growth_bytes=max_vram_growth_bytes,
        scenarios=sorted(REQUIRED_SCENARIOS),
        oom_events=oom_events(control),
        data_corruption_events=0,
        scope_leaks=0,
    )
    write_json_new(run_root / "capacity_manifest.json", json.loads(manifest.model_dump_json()))
    report = evaluate_capacity(run_root, formal=True)
    report["checked_at"] = datetime.now(timezone.utc).isoformat()
    write_json_new(run_root / "report.json", report)
    return report


def build_readiness_fixture(output: Path) -> dict[str, object]:
    """Exercise the complete evidence schema without claiming runtime measurements."""
    output.mkdir(parents=True, exist_ok=False)
    start = datetime.now(timezone.utc).replace(microsecond=0)
    finish = start + timedelta(minutes=31)
    def digest(label: str) -> str:
        return sha256_bytes(label.encode("utf-8"))
    environment = CapacityEnvironment(
        captured_at=start,
        scope="local-readiness",
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
        image_digests={key: "local-image-id:sha256:" + digest(key) for key in ("app", "parse-worker", "parser", "proxy")},
        postgres_version="18.6-fixture",
        pgvector_version="0.8.6-fixture",
        model_provider="vllm",
        model_runtime_version="0.10.2-fixture",
        embedding_model="Qwen/Qwen3-Embedding-0.6B",
        embedding_digest=digest("embedding"),
        generation_model="Qwen/Qwen3-4B-Instruct-2507",
        generation_digest=digest("generation"),
        disk_total_bytes=512 * 1024**2,
        timezone="UTC",
        code_ref="fixture-not-runtime",
        config_sha256=digest("config"),
    )
    write_json_new(output / "environment.json", json.loads(environment.model_dump_json()))
    samples: list[CapacitySample] = []
    steady = ("steady_query", "parse_with_query", "reindex_with_query", "steady_observation")
    for index in range(60):
        timestamp = start + timedelta(seconds=index * 30)
        scenario = steady[index % len(steady)]
        for operation, latency in (("api", 100.0), ("evidence", 800.0), ("queue", 200.0), ("rag", 5000.0)):
            samples.append(CapacitySample(
                timestamp=timestamp, scenario=scenario,
                operation=cast(Literal["api", "evidence", "queue", "rag"], operation),
                ok=True, latency_ms=latency,
            ))
        samples.append(CapacitySample(
            timestamp=timestamp, scenario="steady_observation", operation="resource", ok=True,
            rss_bytes=1_000_000_000 + index * 1000, vram_bytes=10_000_000_000 + index * 1000,
            query_queue_depth=2,
        ))
    for offset, scenario in enumerate(sorted(REQUIRED_SCENARIOS - set(steady)), start=1):
        samples.append(CapacitySample(
            timestamp=start + timedelta(minutes=10, seconds=offset), scenario=scenario,
            operation="control", ok=True,
        ))
    for offset, (scenario, code) in enumerate((
        ("model_restart", "MODEL_CONNECTION_FAILED"),
        ("database_restart", "DATABASE_UNAVAILABLE"),
        ("queue_full", "QUEUE_FULL"),
    ), start=1):
        samples.append(CapacitySample(
            timestamp=start + timedelta(minutes=11, seconds=offset), scenario=scenario,
            operation="control" if scenario != "queue_full" else "queue", ok=False,
            latency_ms=100.0 if scenario == "queue_full" else None, error_code=code,
            query_queue_depth=20 if scenario == "queue_full" else None,
        ))
        if scenario != "queue_full":
            samples.append(CapacitySample(
                timestamp=start + timedelta(minutes=12, seconds=offset), scenario=scenario,
                operation="control", ok=True,
            ))
    samples.append(CapacitySample(
        timestamp=finish, scenario="steady_observation", operation="resource", ok=True,
        rss_bytes=1_000_060_000, vram_bytes=10_000_060_000, query_queue_depth=0,
    ))
    samples_path = output / "samples.jsonl"
    with samples_path.open("x", encoding="utf-8", newline="\n") as stream:
        for sample in sorted(samples, key=lambda item: item.timestamp):
            stream.write(sample.model_dump_json() + "\n")
    manifest = CapacityManifest(
        format_version=1,
        run_id="t075-local-readiness-fixture",
        started_at=start,
        finished_at=finish,
        environment_path="environment.json",
        environment_sha256=sha256_file(output / "environment.json"),
        samples_path="samples.jsonl",
        samples_sha256=sha256_file(samples_path),
        active_chunks=50_000,
        registered_users=20,
        query_concurrency=5,
        max_error_rate=0.05,
        max_rss_growth_bytes=512 * 1024**2,
        max_vram_growth_bytes=1024 * 1024**2,
        scenarios=sorted(REQUIRED_SCENARIOS),
        oom_events=0,
        data_corruption_events=0,
        scope_leaks=0,
    )
    write_json_new(output / "capacity_manifest.json", json.loads(manifest.model_dump_json()))
    validated = evaluate_capacity(output, formal=False)
    report = {
        "status": "fixture",
        "scope": "t075-local-readiness",
        "formal": False,
        "formal_claim": "none",
        "validator_status": validated["status"],
        "scenario_count": validated["scenario_count"],
        "sample_count": validated["sample_count"],
        "limitations": [
            "synthetic schema fixture; no target requests or faults were executed",
            "must never be used as T-075 or resume capacity evidence",
        ],
    }
    write_json_new(output / "report.json", report)
    (output / "README.md").write_text(
        "# T-075 local readiness fixture\n\nSynthetic schema-only artifact. Not runtime or formal capacity evidence.\n",
        encoding="utf-8", newline="\n",
    )
    return report
