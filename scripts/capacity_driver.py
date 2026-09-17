"""Prepare and preflight the real T-075 capacity run.

This command deliberately does not synthesize capacity samples.  It creates a
parser-verified 50,000-chunk corpus and records the three target-host checks
that must pass before the billed formal run is allowed to start.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence, cast
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ingest import chunks, parse
from loadtests.capacity_evidence import (
    build_readiness_fixture, capture_database_proof, capture_environment, finalize_run, formal_preflight,
)
from loadtests.capacity_images import pin_images
from loadtests.capacity_runtime import HostControl, SampleSink, UploadFile, run_workload
from loadtests.capacity_volume import prepare_volume


FORMAL_DOCUMENTS = 20
FORMAL_CHUNKS_PER_DOCUMENT = 2500
FORMAL_ACTIVE_CHUNKS = 50_000
DEFAULT_SEED = 20260916
TERMINAL_JOB_STATES = {"succeeded", "failed", "cancelled", "superseded"}
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def write_json(path: Path, value: object) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def json_object(response: httpx.Response) -> dict[str, object]:
    value = response.json()
    if type(value) is not dict:
        raise RuntimeError("target response was not a JSON object")
    return cast(dict[str, object], value)


def error_code(response: httpx.Response) -> str:
    try:
        body = json_object(response)
        error = body.get("error")
        if type(error) is dict and type(error.get("code")) is str:
            return cast(str, error["code"])
    except (RuntimeError, ValueError):
        pass
    return f"HTTP_STATUS_{response.status_code}"


@dataclass(frozen=True, repr=False)
class TargetCredentials:
    admin_email: str
    admin_password: str
    user_password: str

    @classmethod
    def load(cls, path: Path) -> TargetCredentials:
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 4096:
            raise ValueError("credentials file must be a bounded regular file")
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if type(value) is not dict or set(value) != {"admin_email", "admin_password", "user_password"}:
            raise ValueError("credentials file must contain exactly admin_email, admin_password, user_password")
        admin_email, admin_password, user_password = (
            value["admin_email"], value["admin_password"], value["user_password"]
        )
        if type(admin_email) is not str or EMAIL.fullmatch(admin_email) is None:
            raise ValueError("admin_email is invalid")
        if type(admin_password) is not str or not 12 <= len(admin_password) <= 128:
            raise ValueError("admin_password is invalid")
        if type(user_password) is not str or not 12 <= len(user_password) <= 128:
            raise ValueError("user_password is invalid")
        return cls(admin_email.casefold(), admin_password, user_password)


def prepare_execution_credentials(source: Path, destination: Path) -> Path:
    if destination.exists():
        TargetCredentials.load(destination)
        return destination
    if source.is_symlink() or not source.is_file() or not 0 < source.stat().st_size <= 4096:
        raise ValueError("admin credentials file must be a bounded regular file")
    value = json.loads(source.read_text(encoding="utf-8-sig"))
    if type(value) is not dict:
        raise ValueError("admin credentials file must be a JSON object")
    if set(value) == {"admin_email", "admin_password", "user_password"}:
        credentials = TargetCredentials.load(source)
    elif set(value) == {"email", "password"}:
        email, password = value["email"], value["password"]
        if type(email) is not str or EMAIL.fullmatch(email) is None:
            raise ValueError("admin email is invalid")
        if type(password) is not str or not 12 <= len(password) <= 128:
            raise ValueError("admin password is invalid")
        credentials = TargetCredentials(email.casefold(), password, secrets.token_urlsafe(24))
    else:
        raise ValueError("admin credentials file has unexpected keys")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump({
            "admin_email": credentials.admin_email,
            "admin_password": credentials.admin_password,
            "user_password": credentials.user_password,
        }, stream, ensure_ascii=False)
        stream.write("\n")
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    return destination


class TargetClient:
    def __init__(self, base_url: str, *, verify: bool, timeout: float = 30.0):
        parsed = urlsplit(base_url.rstrip("/"))
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            raise ValueError("base_url must be an HTTP(S) origin without credentials")
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("non-loopback targets must use HTTPS")
        self.base_url = base_url.rstrip("/")
        self.origin = self.base_url
        self.client = httpx.Client(
            base_url=self.base_url,
            verify=verify,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )
        self.csrf = ""

    def close(self) -> None:
        self.client.close()

    def login(self, email: str, password: str) -> None:
        response = self.client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": password},
            headers={"Origin": self.origin},
        )
        if response.status_code != 204:
            raise RuntimeError(f"login failed: {error_code(response)}")
        token = response.headers.get("X-CSRF-Token")
        if not token:
            raise RuntimeError("login response did not include a CSRF token")
        self.csrf = token

    def bootstrap(self, token: str, email: str, password: str) -> None:
        response = self.client.post(
            "/api/v1/auth/bootstrap",
            json={"token": token, "email": email, "password": password, "display_name": "T075 Administrator"},
            headers={"Origin": self.origin},
        )
        if response.status_code not in {201, 409}:
            raise RuntimeError(f"bootstrap failed: {error_code(response)}")

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        json: object | None = None,
        files: dict[str, UploadFile] | None = None,
        data: dict[str, str] | None = None,
    ) -> httpx.Response:
        request_headers = dict(headers or {})
        if method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            request_headers.update({"Origin": self.origin, "X-CSRF-Token": self.csrf})
        return self.client.request(
            method,
            path,
            headers=request_headers,
            json=json,
            files=files,
            data=data,
        )


def require_status(response: httpx.Response, expected: set[int]) -> dict[str, object]:
    if response.status_code not in expected:
        raise RuntimeError(f"target request failed: {error_code(response)}")
    return json_object(response) if response.content else {}


def capacity_document(document_index: int, *, chunks_per_document: int, seed: int) -> bytes:
    """Return deterministic synthetic text with one parser chunk per section."""
    if not 1 <= document_index <= 999:
        raise ValueError("document_index must be between 1 and 999")
    if not 1 <= chunks_per_document <= FORMAL_CHUNKS_PER_DOCUMENT:
        raise ValueError("chunks_per_document must be between 1 and 2500")
    text = "".join(
        f"## D{document_index:02d} S{section:04d}\n"
        f"capacity seed {seed} document {document_index:02d} section {section:04d} "
        f"{'z' * 330}\n\n"
        for section in range(1, chunks_per_document + 1)
    )
    return text.encode("utf-8")


def prepare_corpus(
    output: Path,
    *,
    documents: int = FORMAL_DOCUMENTS,
    chunks_per_document: int = FORMAL_CHUNKS_PER_DOCUMENT,
    seed: int = DEFAULT_SEED,
) -> dict[str, object]:
    if not 1 <= documents <= FORMAL_DOCUMENTS:
        raise ValueError("documents must be between 1 and 20")
    if not 1 <= chunks_per_document <= FORMAL_CHUNKS_PER_DOCUMENT:
        raise ValueError("chunks_per_document must be between 1 and 2500")
    output.mkdir(parents=True, exist_ok=False)
    corpus = output / "corpus"
    corpus.mkdir()
    records: list[dict[str, object]] = []
    total_chunks = 0
    total_bytes = 0
    for document_index in range(1, documents + 1):
        name = f"capacity-{document_index:02d}.md"
        data = capacity_document(document_index, chunks_per_document=chunks_per_document, seed=seed)
        path = corpus / name
        with path.open("xb") as stream:
            stream.write(data)
        clean_name, pages = parse(name, data)
        parsed = chunks(pages)
        if clean_name != name or len(parsed) != chunks_per_document:
            raise RuntimeError(f"parser chunk count mismatch for {name}")
        records.append({
            "path": f"corpus/{name}",
            "sha256": sha256_file(path),
            "size_bytes": len(data),
            "chunk_count": len(parsed),
        })
        total_chunks += len(parsed)
        total_bytes += len(data)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "scope": "t075-capacity-corpus",
        "formal_claim": "none",
        "generated_at": utc_now(),
        "seed": seed,
        "documents": records,
        "document_count": len(records),
        "chunks_per_document": chunks_per_document,
        "total_chunks": total_chunks,
        "total_bytes": total_bytes,
        "formal_shape": (
            documents == FORMAL_DOCUMENTS
            and chunks_per_document == FORMAL_CHUNKS_PER_DOCUMENT
            and total_chunks == FORMAL_ACTIVE_CHUNKS
        ),
        "parser_verified": True,
    }
    write_json_new(output / "corpus_manifest.json", manifest)
    return manifest


Runner = Callable[..., subprocess.CompletedProcess[str]]


def _run_check(
    check_id: str,
    command: Sequence[str],
    *,
    runner: Runner = subprocess.run,
    timeout: int = 60,
) -> dict[str, object]:
    started_at = utc_now()
    try:
        result = runner(
            list(command),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "id": check_id,
            "status": "failed",
            "started_at": started_at,
            "finished_at": utc_now(),
            "error_type": type(exc).__name__,
        }
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    return {
        "id": check_id,
        "status": "passed" if result.returncode == 0 else "failed",
        "started_at": started_at,
        "finished_at": utc_now(),
        "exit_code": result.returncode,
        "stdout": stdout[:4000],
        "stderr": stderr[:4000],
    }


def preflight(output: Path, *, runner: Runner = subprocess.run) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=False)
    checks = [
        _run_check(
            "nvidia-smi",
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,memory.total",
                "--format=csv,noheader,nounits",
            ],
            runner=runner,
        ),
        _run_check("docker-compose", ["docker", "compose", "version"], runner=runner),
        _run_check(
            "parser-sandbox",
            ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL", "alpine", "true"],
            runner=runner,
            timeout=120,
        ),
    ]
    report: dict[str, object] = {
        "schema_version": 1,
        "scope": "t075-target-preflight",
        "formal_claim": "none",
        "captured_at": utc_now(),
        "host": platform.node(),
        "status": "passed" if all(check["status"] == "passed" for check in checks) else "failed",
        "checks": checks,
        "next_step": "prepare-corpus" if all(check["status"] == "passed" for check in checks) else "stop-before-billed-run",
    }
    write_json_new(output / "preflight.json", report)
    return report


def _items(body: dict[str, object]) -> list[dict[str, object]]:
    value = body.get("items")
    if type(value) is not list or any(type(item) is not dict for item in value):
        raise RuntimeError("target page did not contain object items")
    return cast(list[dict[str, object]], value)


def _wait_jobs(
    client: TargetClient,
    job_ids: set[str],
    *,
    status_path: Path,
    timeout_seconds: int,
) -> list[dict[str, object]]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, dict[str, object]] = {}
    while job_ids - latest.keys() or any(item.get("state") not in TERMINAL_JOB_STATES for item in latest.values()):
        if time.monotonic() >= deadline:
            raise RuntimeError("target jobs did not finish before the provisioning deadline")
        for job_id in sorted(job_ids):
            body = require_status(client.request("GET", f"/api/v1/jobs/{job_id}"), {200})
            latest[job_id] = body
        snapshot = {
            "schema_version": 1,
            "scope": "t075-target-provision",
            "formal_claim": "none",
            "status": "running",
            "updated_at": utc_now(),
            "jobs": [latest[job_id] for job_id in sorted(latest)],
        }
        write_json(status_path, snapshot)
        if all(item.get("state") in TERMINAL_JOB_STATES for item in latest.values()):
            break
        time.sleep(2)
    failed = [item for item in latest.values() if item.get("state") != "succeeded"]
    if failed:
        codes = sorted({str(item.get("error_code") or item.get("state")) for item in failed})
        raise RuntimeError("target jobs failed: " + ",".join(codes))
    return [latest[job_id] for job_id in sorted(latest)]


def _find_or_create_kb(client: TargetClient, workspace_id: str, slug: str) -> str:
    page = require_status(client.request("GET", f"/api/v1/workspaces/{workspace_id}/knowledge-bases?limit=200"), {200})
    for item in _items(page):
        if item.get("slug") == slug and type(item.get("id")) is str:
            return cast(str, item["id"])
    body = require_status(
        client.request(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/knowledge-bases",
            json={"name": "T-075 Capacity", "slug": slug},
        ),
        {201},
    )
    identifier = body.get("id")
    if type(identifier) is not str:
        raise RuntimeError("knowledge-base creation did not return an id")
    return identifier


def _ensure_users(
    client: TargetClient,
    workspace_id: str,
    kb_id: str,
    password: str,
    *,
    count: int = 19,
) -> list[str]:
    page = require_status(client.request("GET", f"/api/v1/workspaces/{workspace_id}/users?limit=200"), {200})
    existing = {str(item.get("email")): str(item.get("id")) for item in _items(page)}
    user_ids: list[str] = []
    for index in range(1, count + 1):
        email = f"t075-user-{index:02d}@example.invalid"
        user_id = existing.get(email)
        if user_id is None:
            body = require_status(
                client.request(
                    "POST",
                    "/api/v1/users",
                    json={"email": email, "password": password, "display_name": f"T075 User {index:02d}"},
                ),
                {201},
            )
            value = body.get("id")
            if type(value) is not str:
                raise RuntimeError("user creation did not return an id")
            user_id = value
        require_status(
            client.request("PUT", f"/api/v1/workspaces/{workspace_id}/members/{user_id}", json={"role": "member"}),
            {204},
        )
        require_status(
            client.request("PUT", f"/api/v1/knowledge-bases/{kb_id}/members/{user_id}", json={"role": "viewer"}),
            {204},
        )
        user_ids.append(user_id)
    return user_ids


def _verify_corpus(corpus_root: Path) -> tuple[dict[str, object], list[Path]]:
    manifest_path = corpus_root / "corpus_manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("corpus manifest is missing")
    manifest_value = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if type(manifest_value) is not dict:
        raise ValueError("corpus manifest must be an object")
    manifest = cast(dict[str, object], manifest_value)
    if (manifest.get("formal_shape") is not True or manifest.get("parser_verified") is not True
            or manifest.get("total_chunks") != FORMAL_ACTIVE_CHUNKS or manifest.get("document_count") != FORMAL_DOCUMENTS):
        raise ValueError("provision requires the parser-verified formal corpus shape")
    records = manifest.get("documents")
    if type(records) is not list or len(records) != FORMAL_DOCUMENTS:
        raise ValueError("corpus document records are invalid")
    paths: list[Path] = []
    for record_value in records:
        if type(record_value) is not dict:
            raise ValueError("corpus document record is invalid")
        record = cast(dict[str, object], record_value)
        relative, digest = record.get("path"), record.get("sha256")
        if type(relative) is not str or type(digest) is not str or "\\" in relative or Path(relative).is_absolute():
            raise ValueError("corpus path is invalid")
        path = corpus_root / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(corpus_root.resolve()):
            raise ValueError("corpus file is missing or outside its root")
        if sha256_file(path) != digest:
            raise ValueError("corpus file hash mismatch")
        paths.append(path)
    return manifest, paths


def provision_target(
    output: Path,
    *,
    base_url: str,
    credentials_path: Path,
    corpus_root: Path,
    verify_tls: bool,
    slug: str,
    timeout_seconds: int,
    bootstrap_token_path: Path | None = None,
) -> dict[str, object]:
    credentials = TargetCredentials.load(credentials_path)
    corpus_manifest, corpus_paths = _verify_corpus(corpus_root)
    output.mkdir(parents=True, exist_ok=False)
    status_path = output / "status.json"
    status: dict[str, object] = {
        "schema_version": 1,
        "scope": "t075-target-provision",
        "formal_claim": "none",
        "status": "running",
        "started_at": utc_now(),
        "last_stage": "login",
        "corpus_manifest_sha256": sha256_file(corpus_root / "corpus_manifest.json"),
    }
    write_json_new(status_path, status)
    client = TargetClient(base_url, verify=verify_tls)
    try:
        try:
            client.login(credentials.admin_email, credentials.admin_password)
        except RuntimeError:
            if bootstrap_token_path is None:
                raise
            if (bootstrap_token_path.is_symlink() or not bootstrap_token_path.is_file()
                    or not 0 < bootstrap_token_path.stat().st_size <= 1024):
                raise ValueError("bootstrap token file must be a bounded regular file")
            token = bootstrap_token_path.read_text(encoding="utf-8-sig").strip()
            if not 32 <= len(token) <= 256:
                raise ValueError("bootstrap token is invalid")
            client.bootstrap(token, credentials.admin_email, credentials.admin_password)
            client.login(credentials.admin_email, credentials.admin_password)
        me = require_status(client.request("GET", "/api/v1/me"), {200})
        workspaces = me.get("workspaces")
        if type(workspaces) is not list or not workspaces or type(workspaces[0]) is not dict:
            raise RuntimeError("admin has no target workspace")
        workspace_id = workspaces[0].get("id")
        if type(workspace_id) is not str:
            raise RuntimeError("target workspace id is invalid")
        status["last_stage"] = "knowledge-base"
        write_json(status_path, status)
        kb_id = _find_or_create_kb(client, workspace_id, slug)
        status.update({"workspace_id": workspace_id, "kb_id": kb_id, "last_stage": "users"})
        write_json(status_path, status)
        users = _ensure_users(client, workspace_id, kb_id, credentials.user_password)
        status.update({"registered_capacity_users": len(users) + 1, "last_stage": "documents"})
        write_json(status_path, status)

        page = require_status(client.request("GET", f"/api/v1/knowledge-bases/{kb_id}/documents?limit=200"), {200})
        existing_documents = {str(item.get("name")) for item in _items(page)}
        parse_job_ids: set[str] = set()
        for index, path in enumerate(corpus_paths, start=1):
            if path.name in existing_documents:
                continue
            with path.open("rb") as stream:
                response = client.request(
                    "POST",
                    f"/api/v1/knowledge-bases/{kb_id}/documents",
                    headers={"Idempotency-Key": f"t075-{slug}-document-{index:02d}"},
                    files={"file": (path.name, stream, "text/markdown")},
                    data={"display_name": path.name},
                )
            body = require_status(response, {202})
            job_id = body.get("job_id")
            if type(job_id) is str:
                parse_job_ids.add(job_id)
        if parse_job_ids:
            status["last_stage"] = "parse-jobs"
            write_json(status_path, status)
            _wait_jobs(client, parse_job_ids, status_path=status_path, timeout_seconds=timeout_seconds)

        status["last_stage"] = "automatic-index"
        write_json(status_path, status)
        jobs_page = require_status(client.request("GET", f"/api/v1/jobs?kb_id={kb_id}&limit=200"), {200})
        index_jobs = [item for item in _items(jobs_page) if item.get("type") in {"index", "reindex"}]
        if not index_jobs or type(index_jobs[0].get("id")) is not str:
            raise RuntimeError("automatic index job was not observed")
        index_job_id = cast(str, index_jobs[0]["id"])
        if index_jobs[0].get("state") not in TERMINAL_JOB_STATES:
            _wait_jobs(client, {index_job_id}, status_path=status_path, timeout_seconds=timeout_seconds)
        elif index_jobs[0].get("state") != "succeeded":
            raise RuntimeError("automatic index job did not succeed")
        kb = require_status(client.request("GET", f"/api/v1/knowledge-bases/{kb_id}"), {200})
        if type(kb.get("active_index_generation_id")) is not str:
            raise RuntimeError("target knowledge base has no active index generation")
        status.update({
            "status": "prepared",
            "finished_at": utc_now(),
            "last_stage": "complete",
            "document_count": corpus_manifest["document_count"],
            "declared_chunks": corpus_manifest["total_chunks"],
            "active_index_generation_id": kb["active_index_generation_id"],
            "index_job_id": index_job_id,
            "limitations": [
                "active chunk and user counts still require direct SQL capture before the formal run",
                "provisioning is readiness evidence and not a capacity result",
            ],
        })
        write_json(status_path, status)
        return status
    except Exception as exc:
        status.update({
            "status": "failed",
            "finished_at": utc_now(),
            "error_type": type(exc).__name__,
        })
        write_json(status_path, status)
        raise
    finally:
        client.close()


def _read_object(path: Path, label: str) -> dict[str, object]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError(f"{label} must be a bounded regular file")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if type(value) is not dict:
        raise ValueError(f"{label} must be a JSON object")
    return cast(dict[str, object], value)


def run_target(
    output: Path,
    *,
    base_url: str,
    credentials_path: Path,
    provision_status_path: Path,
    project_dir: Path,
    compose_files: tuple[Path, ...],
    env_file: Path,
    project_name: str,
    data_dir: Path,
    duration_seconds: int,
    verify_tls: bool,
    max_error_rate: float,
    max_rss_growth_bytes: int,
    max_vram_growth_bytes: int,
    code_ref: str | None = None,
) -> dict[str, object]:
    if duration_seconds < 1800:
        raise ValueError("formal run duration must be at least 1800 seconds")
    if not 0 <= max_error_rate <= 0.05:
        raise ValueError("max_error_rate must be between 0 and 0.05")
    if max_rss_growth_bytes < 0 or max_vram_growth_bytes < 0:
        raise ValueError("memory growth limits must be nonnegative")
    credentials = TargetCredentials.load(credentials_path)
    provision = _read_object(provision_status_path, "provision status")
    if provision.get("status") != "prepared" or type(provision.get("kb_id")) is not str:
        raise ValueError("provision status is not prepared")
    kb_id = cast(str, provision["kb_id"])
    resolved_project = project_dir.resolve()
    if not resolved_project.is_dir() or env_file.is_symlink() or not env_file.is_file():
        raise ValueError("compose project or env file is missing")
    if not compose_files or any(path.is_symlink() or not path.is_file() for path in compose_files):
        raise ValueError("compose files are missing")
    if data_dir.is_symlink() or not data_dir.is_dir():
        raise ValueError("capacity data directory must be a real directory")
    control = HostControl(
        project_dir=resolved_project,
        compose_files=tuple(path.resolve() for path in compose_files),
        env_file=env_file.resolve(),
        project_name=project_name,
    )
    capture_environment(
        output,
        control=control,
        data_dir=data_dir.resolve(),
        code_ref_fallback=code_ref,
    )
    proof = capture_database_proof(output, control=control, kb_id=kb_id)
    status_path = output / "status.json"
    status: dict[str, object] = {
        "schema_version": 1,
        "scope": "t075-target-capacity",
        "formal_claim": "pending",
        "status": "running",
        "started_at": utc_now(),
        "last_stage": "login-users",
        "kb_id": kb_id,
    }
    write_json_new(status_path, status)
    clients: list[TargetClient] = []
    try:
        admin = TargetClient(base_url, verify=verify_tls, timeout=180)
        admin.login(credentials.admin_email, credentials.admin_password)
        clients.append(admin)
        for index in range(1, 20):
            client = TargetClient(base_url, verify=verify_tls, timeout=180)
            client.login(f"t075-user-{index:02d}@example.invalid", credentials.user_password)
            clients.append(client)
        status["last_stage"] = "formal-workload"
        write_json(status_path, status)
        with SampleSink(output / "samples.jsonl") as sink:
            result = run_workload(
                admin=admin,
                clients=clients,
                sink=sink,
                control=control,
                kb_id=kb_id,
                data_dir=data_dir.resolve(),
                duration_seconds=duration_seconds,
                concurrency=5,
            )
        status["last_stage"] = "formal-report"
        write_json(status_path, status)
        report = finalize_run(
            output,
            control=control,
            result=result,
            database_proof=proof,
            max_error_rate=max_error_rate,
            max_rss_growth_bytes=max_rss_growth_bytes,
            max_vram_growth_bytes=max_vram_growth_bytes,
        )
        status.update({
            "status": report["status"],
            "formal_claim": "capacity-report",
            "finished_at": utc_now(),
            "last_stage": "complete",
            "report_failures": report.get("failures", []),
            "sample_count": result.sample_count,
            "scenario_counts": result.scenario_counts,
        })
        write_json(status_path, status)
        return report
    except Exception as exc:
        status.update({
            "status": "failed",
            "formal_claim": "none",
            "finished_at": utc_now(),
            "error_type": type(exc).__name__,
        })
        write_json(status_path, status)
        raise
    finally:
        for client in clients:
            client.close()


def _control(
    project_dir: Path,
    compose_files: tuple[Path, ...],
    env_file: Path,
    project_name: str,
) -> HostControl:
    if not project_dir.resolve().is_dir() or env_file.is_symlink() or not env_file.is_file():
        raise ValueError("compose project or env file is missing")
    if not compose_files or any(path.is_symlink() or not path.is_file() for path in compose_files):
        raise ValueError("compose files are missing")
    return HostControl(
        project_dir=project_dir.resolve(),
        compose_files=tuple(path.resolve() for path in compose_files),
        env_file=env_file.resolve(),
        project_name=project_name,
    )


def execute_target(
    output: Path,
    *,
    base_url: str,
    credentials_path: Path,
    corpus_root: Path,
    project_dir: Path,
    compose_files: tuple[Path, ...],
    source_env: Path,
    capacity_env: Path,
    project_name: str,
    volume_image: Path,
    data_dir: Path,
    duration_seconds: int,
    verify_tls: bool,
    max_error_rate: float,
    max_rss_growth_bytes: int,
    max_vram_growth_bytes: int,
    bootstrap_token_path: Path,
    code_ref: str | None = None,
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=False)
    status_path = output / "status.json"
    status: dict[str, object] = {
        "schema_version": 1,
        "scope": "t075-execution",
        "formal_claim": "pending",
        "status": "running",
        "started_at": utc_now(),
        "last_stage": "bounded-volume",
    }
    write_json_new(status_path, status)
    source_control = _control(project_dir, compose_files, source_env, project_name)
    try:
        execution_credentials = prepare_execution_credentials(
            credentials_path,
            capacity_env.parent / ".t075-capacity-credentials.json",
        )
        prepare_volume(
            output / "volume",
            image_path=volume_image,
            mount_point=data_dir,
            size_mib=512,
        )
        status["last_stage"] = "pin-images"
        write_json(status_path, status)
        pin_images(
            output / "images",
            control=source_control,
            destination_env=capacity_env,
            data_dir=data_dir,
            tag=output.name[-40:],
        )
        capacity_control = _control(project_dir, compose_files, capacity_env, project_name)
        status["last_stage"] = "start-services"
        write_json(status_path, status)
        capacity_control.run([
            "sudo", "-n", "systemctl", "start",
            "tracedesk-vllm-generation.service", "tracedesk-vllm-embedding.service",
        ], timeout=600)
        capacity_control.compose("up", "--detach", "--wait", "--wait-timeout", "300", timeout=600)
        status["last_stage"] = "formal-preflight"
        write_json(status_path, status)
        preflight_report = formal_preflight(
            output / "preflight",
            control=capacity_control,
            data_dir=data_dir,
            base_url=base_url,
            verify_tls=verify_tls,
        )
        if preflight_report["status"] != "passed":
            raise RuntimeError("formal preflight failed")
        status["last_stage"] = "provision"
        write_json(status_path, status)
        provision_target(
            output / "provision",
            base_url=base_url,
            credentials_path=execution_credentials,
            corpus_root=corpus_root,
            verify_tls=verify_tls,
            slug="t075-capacity",
            timeout_seconds=28_800,
            bootstrap_token_path=bootstrap_token_path,
        )
        status["last_stage"] = "formal-run"
        write_json(status_path, status)
        report = run_target(
            output / "run",
            base_url=base_url,
            credentials_path=execution_credentials,
            provision_status_path=output / "provision" / "status.json",
            project_dir=project_dir,
            compose_files=compose_files,
            env_file=capacity_env,
            project_name=project_name,
            data_dir=data_dir,
            duration_seconds=duration_seconds,
            verify_tls=verify_tls,
            max_error_rate=max_error_rate,
            max_rss_growth_bytes=max_rss_growth_bytes,
            max_vram_growth_bytes=max_vram_growth_bytes,
            code_ref=code_ref,
        )
        status.update({
            "status": report["status"],
            "formal_claim": "capacity-report",
            "finished_at": utc_now(),
            "last_stage": "complete",
            "report_failures": report.get("failures", []),
        })
        write_json(status_path, status)
        return report
    except Exception as exc:
        status.update({
            "status": "failed",
            "formal_claim": "none",
            "finished_at": utc_now(),
            "error_type": type(exc).__name__,
        })
        write_json(status_path, status)
        raise


def build_launch_bundle(output: Path, *, corpus_root: Path) -> dict[str, object]:
    _verify_corpus(corpus_root)
    output.mkdir(parents=True, exist_ok=False)
    source_files = [
        ROOT / "app" / "__init__.py",
        ROOT / "app" / "ingest.py",
        ROOT / "scripts" / "__init__.py",
        ROOT / "scripts" / "capacity_driver.py",
        ROOT / "scripts" / "capacity_report.py",
        ROOT / "scripts" / "t075_target_execute.sh",
        ROOT / "loadtests" / "__init__.py",
        ROOT / "loadtests" / "capacity_runtime.py",
        ROOT / "loadtests" / "capacity_evidence.py",
        ROOT / "loadtests" / "capacity_images.py",
        ROOT / "loadtests" / "capacity_volume.py",
        ROOT / "app" / "operations" / "__init__.py",
        ROOT / "app" / "operations" / "capacity.py",
        corpus_root / "corpus_manifest.json",
        *sorted((corpus_root / "corpus").glob("*.md")),
    ]
    if any(path.is_symlink() or not path.is_file() for path in source_files):
        raise ValueError("launch bundle input is missing or uses a symlink")
    members: list[dict[str, object]] = []
    archive = output / "t075-launch-bundle.tar.gz"
    with tarfile.open(archive, "x:gz") as stream:
        for path in source_files:
            if path.is_relative_to(corpus_root):
                relative = Path("artifacts/t075-readiness") / path.relative_to(corpus_root)
            else:
                relative = path.relative_to(ROOT)
            stream.add(path, arcname=relative.as_posix(), recursive=False)
            members.append({
                "path": relative.as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            })
    report: dict[str, object] = {
        "schema_version": 1,
        "scope": "t075-launch-bundle",
        "formal_claim": "none",
        "status": "prepared",
        "contains_credentials": False,
        "contains_private_evaluation_data": False,
        "member_count": len(members),
        "members": members,
        "archive": {
            "path": archive.name,
            "size_bytes": archive.stat().st_size,
            "sha256": sha256_file(archive),
        },
    }
    write_json_new(output / "manifest.json", report)
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    corpus = subparsers.add_parser("prepare-corpus")
    corpus.add_argument("--output", type=Path, required=True)
    corpus.add_argument("--documents", type=int, default=FORMAL_DOCUMENTS)
    corpus.add_argument("--chunks-per-document", type=int, default=FORMAL_CHUNKS_PER_DOCUMENT)
    corpus.add_argument("--seed", type=int, default=DEFAULT_SEED)
    target = subparsers.add_parser("preflight")
    target.add_argument("--output", type=Path, required=True)
    readiness = subparsers.add_parser("dry-run")
    readiness.add_argument("--output", type=Path, required=True)
    bundle = subparsers.add_parser("build-bundle")
    bundle.add_argument("--output", type=Path, required=True)
    bundle.add_argument("--corpus-root", type=Path, required=True)
    provision = subparsers.add_parser("provision")
    provision.add_argument("--output", type=Path, required=True)
    provision.add_argument("--base-url", required=True)
    provision.add_argument("--credentials-file", type=Path, required=True)
    provision.add_argument("--corpus-root", type=Path, required=True)
    provision.add_argument("--slug", default="t075-capacity")
    provision.add_argument("--timeout-seconds", type=int, default=14_400)
    provision.add_argument("--bootstrap-token-file", type=Path)
    provision.add_argument("--insecure", action="store_true")
    run = subparsers.add_parser("run")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--base-url", required=True)
    run.add_argument("--credentials-file", type=Path, required=True)
    run.add_argument("--provision-status", type=Path, required=True)
    run.add_argument("--project-dir", type=Path, required=True)
    run.add_argument("--compose-file", type=Path, action="append", required=True)
    run.add_argument("--env-file", type=Path, required=True)
    run.add_argument("--project-name", default="tracedesk-t075")
    run.add_argument("--data-dir", type=Path, required=True)
    run.add_argument("--duration-seconds", type=int, default=1800)
    run.add_argument("--max-error-rate", type=float, default=0.05)
    run.add_argument("--max-rss-growth-bytes", type=int, default=512 * 1024 * 1024)
    run.add_argument("--max-vram-growth-bytes", type=int, default=1024 * 1024 * 1024)
    run.add_argument("--code-ref")
    run.add_argument("--insecure", action="store_true")
    pin = subparsers.add_parser("pin-images")
    pin.add_argument("--output", type=Path, required=True)
    pin.add_argument("--project-dir", type=Path, required=True)
    pin.add_argument("--compose-file", type=Path, action="append", required=True)
    pin.add_argument("--source-env", type=Path, required=True)
    pin.add_argument("--destination-env", type=Path, required=True)
    pin.add_argument("--project-name", default="tracedesk-t075")
    pin.add_argument("--data-dir", type=Path)
    pin.add_argument("--tag", default="t075")
    volume = subparsers.add_parser("prepare-volume")
    volume.add_argument("--output", type=Path, required=True)
    volume.add_argument("--image-path", type=Path, required=True)
    volume.add_argument("--mount-point", type=Path, required=True)
    volume.add_argument("--size-mib", type=int, default=512)
    formal = subparsers.add_parser("formal-preflight")
    formal.add_argument("--output", type=Path, required=True)
    formal.add_argument("--base-url", required=True)
    formal.add_argument("--project-dir", type=Path, required=True)
    formal.add_argument("--compose-file", type=Path, action="append", required=True)
    formal.add_argument("--env-file", type=Path, required=True)
    formal.add_argument("--project-name", default="tracedesk-t075")
    formal.add_argument("--data-dir", type=Path, required=True)
    formal.add_argument("--insecure", action="store_true")
    execute = subparsers.add_parser("execute")
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--base-url", required=True)
    execute.add_argument("--credentials-file", type=Path, required=True)
    execute.add_argument("--bootstrap-token-file", type=Path, required=True)
    execute.add_argument("--corpus-root", type=Path, required=True)
    execute.add_argument("--project-dir", type=Path, required=True)
    execute.add_argument("--compose-file", type=Path, action="append", required=True)
    execute.add_argument("--source-env", type=Path, required=True)
    execute.add_argument("--capacity-env", type=Path, required=True)
    execute.add_argument("--project-name", default="tracedesk-t075")
    execute.add_argument("--volume-image", type=Path, required=True)
    execute.add_argument("--data-dir", type=Path, required=True)
    execute.add_argument("--duration-seconds", type=int, default=1800)
    execute.add_argument("--max-error-rate", type=float, default=0.05)
    execute.add_argument("--max-rss-growth-bytes", type=int, default=512 * 1024 * 1024)
    execute.add_argument("--max-vram-growth-bytes", type=int, default=1024 * 1024 * 1024)
    execute.add_argument("--code-ref")
    execute.add_argument("--insecure", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "prepare-corpus":
            report = prepare_corpus(
                args.output,
                documents=args.documents,
                chunks_per_document=args.chunks_per_document,
                seed=args.seed,
            )
        elif args.command == "preflight":
            report = preflight(args.output)
        elif args.command == "dry-run":
            report = build_readiness_fixture(args.output)
        elif args.command == "build-bundle":
            report = build_launch_bundle(args.output, corpus_root=args.corpus_root)
        elif args.command == "provision":
            if not 60 <= args.timeout_seconds <= 28_800:
                raise ValueError("timeout-seconds must be between 60 and 28800")
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", args.slug) is None:
                raise ValueError("slug is invalid")
            report = provision_target(
                args.output,
                base_url=args.base_url,
                credentials_path=args.credentials_file,
                corpus_root=args.corpus_root,
                verify_tls=not args.insecure,
                slug=args.slug,
                timeout_seconds=args.timeout_seconds,
                bootstrap_token_path=args.bootstrap_token_file,
            )
        elif args.command == "run":
            report = run_target(
                args.output,
                base_url=args.base_url,
                credentials_path=args.credentials_file,
                provision_status_path=args.provision_status,
                project_dir=args.project_dir,
                compose_files=tuple(args.compose_file),
                env_file=args.env_file,
                project_name=args.project_name,
                data_dir=args.data_dir,
                duration_seconds=args.duration_seconds,
                verify_tls=not args.insecure,
                max_error_rate=args.max_error_rate,
                max_rss_growth_bytes=args.max_rss_growth_bytes,
                max_vram_growth_bytes=args.max_vram_growth_bytes,
                code_ref=args.code_ref,
            )
        elif args.command == "pin-images":
            report = pin_images(
                args.output,
                control=_control(args.project_dir, tuple(args.compose_file), args.source_env, args.project_name),
                destination_env=args.destination_env,
                data_dir=args.data_dir,
                tag=args.tag,
            )
        elif args.command == "prepare-volume":
            report = prepare_volume(
                args.output,
                image_path=args.image_path,
                mount_point=args.mount_point,
                size_mib=args.size_mib,
            )
        elif args.command == "formal-preflight":
            report = formal_preflight(
                args.output,
                control=_control(args.project_dir, tuple(args.compose_file), args.env_file, args.project_name),
                data_dir=args.data_dir,
                base_url=args.base_url,
                verify_tls=not args.insecure,
            )
        else:
            report = execute_target(
                args.output,
                base_url=args.base_url,
                credentials_path=args.credentials_file,
                corpus_root=args.corpus_root,
                project_dir=args.project_dir,
                compose_files=tuple(args.compose_file),
                source_env=args.source_env,
                capacity_env=args.capacity_env,
                project_name=args.project_name,
                volume_image=args.volume_image,
                data_dir=args.data_dir,
                duration_seconds=args.duration_seconds,
                verify_tls=not args.insecure,
                max_error_rate=args.max_error_rate,
                max_rss_growth_bytes=args.max_rss_growth_bytes,
                max_vram_growth_bytes=args.max_vram_growth_bytes,
                bootstrap_token_path=args.bootstrap_token_file,
                code_ref=args.code_ref,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"capacity driver failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({
        "status": report.get("status", "prepared"),
        "scope": report.get("scope", "t075-target-capacity"),
        "formal_claim": report.get("formal_claim", "capacity-report" if args.command in {"run", "execute"} else "none"),
        "total_chunks": report.get("total_chunks"),
    }, ensure_ascii=False))
    return 0 if report.get("status", "passed") != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
