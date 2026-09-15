import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.portfolio_report import PortfolioEnvironment, PortfolioEvidenceError, sha256_file, validate_portfolio_run
from scripts.portfolio_smoke import _fixture_environment, build_fixture_corpus, run_dry_run, run_real_health


def test_fixture_corpus_uses_real_parser_and_exact_counts(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    manifest = build_fixture_corpus(run, documents=3, chunks_per_document=4, seed=17)

    assert manifest.total_chunks == 12
    assert [item.chunk_count for item in manifest.documents] == [4, 4, 4]
    for item in manifest.documents:
        path = run / Path(item.path)
        assert path.is_file()
        assert path.stat().st_size == item.size_bytes
        assert sha256_file(path) == item.sha256


def test_dry_run_is_a_fixture_and_contains_bounded_evidence_shape(tmp_path):
    run = tmp_path / "run"
    report = run_dry_run(run, documents=2, chunks_per_document=5, registered_users=3, query_concurrency=5)

    assert report["status"] == "fixture"
    assert report["scope"] == "portfolio-smoke"
    assert report["formal"] is False
    assert report["formal_claim"] == "none"
    assert report["generated_fixture_chunks"] == 10
    assert report["active_chunks"] is None
    assert report["coverage"] == "harness-fixture"
    assert report["registered_users"] is None
    assert report["fixture_registered_users"] == 3
    assert report["query_concurrency"] is None
    assert report["fixture_query_concurrency"] == 5
    assert report["latency"] == {}
    assert report["rss_growth_bytes"] is None
    assert report["metrics_status"] == "fixture-only"
    assert report["resource_collection"] == "fixture"
    assert (run / "portfolio_manifest.json").is_file()
    assert (run / "samples.jsonl").is_file()


def test_dry_run_cannot_be_reported_without_explicit_fixture_flag(tmp_path):
    run = tmp_path / "run"
    run_dry_run(run, documents=1, chunks_per_document=2)

    with pytest.raises(PortfolioEvidenceError, match="DRY_RUN_NOT_EVIDENCE"):
        validate_portfolio_run(run)


def test_sample_hash_tampering_is_rejected(tmp_path):
    run = tmp_path / "run"
    run_dry_run(run, documents=1, chunks_per_document=2)
    samples = run / "samples.jsonl"
    samples.write_text(samples.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    with pytest.raises(PortfolioEvidenceError, match="samples hash mismatch"):
        validate_portfolio_run(run, allow_fixture=True)


def test_corpus_file_tampering_is_rejected(tmp_path):
    run = tmp_path / "run"
    run_dry_run(run, documents=1, chunks_per_document=2)
    corpus_manifest = json.loads((run / "corpus_manifest.json").read_text(encoding="utf-8"))
    path = run / Path(corpus_manifest["documents"][0]["path"])
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(PortfolioEvidenceError, match="Corpus file hash or size mismatch"):
        validate_portfolio_run(run, allow_fixture=True)


def test_environment_sensitive_fields_are_rejected_after_hash_update(tmp_path):
    run = tmp_path / "run"
    run_dry_run(run, documents=1, chunks_per_document=2)
    environment_path = run / "environment.json"
    payload = json.loads(environment_path.read_text(encoding="utf-8"))
    payload["token"] = "must-not-be-recorded"
    environment_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest_path = run / "portfolio_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["environment_sha256"] = sha256_file(environment_path)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    with pytest.raises(PortfolioEvidenceError, match="Forbidden sensitive field"):
        validate_portfolio_run(run, allow_fixture=True)


def test_real_environment_template_cannot_be_used_as_evidence():
    root = Path(__file__).resolve().parents[2]
    template = root / "docs" / "industrial" / "portfolio-environment.example.json"

    with pytest.raises(ValueError, match="fixture or REPLACE"):
        PortfolioEnvironment.model_validate_json(template.read_text(encoding="utf-8"))


def test_public_real_environment_requires_verified_tls():
    environment = _fixture_environment().model_copy(update={
        "run_mode": "real", "host": "public-test", "os": "Linux", "kernel": "kernel",
        "cpu": "cpu", "gpu": "gpu", "gpu_driver": "driver", "docker_version": "docker",
        "compose_version": "compose", "model_runtime_version": "vllm-0.10.2", "code_ref": "public-test",
        "access_mode": "public", "resource_collection": "not-collected", "tls_verification": "disabled",
    })

    with pytest.raises(ValueError, match="verified TLS"):
        PortfolioEnvironment.model_validate_json(environment.model_dump_json())


class _HealthHandler(BaseHTTPRequestHandler):
    status = 200

    def do_GET(self):
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b"sensitive-response-body-not-to-save")

    def log_message(self, _format, *args):
        pass


@pytest.mark.parametrize("status, expected", [(200, "health-only"), (503, "failed")])
def test_local_runtime_health_is_not_claimed_as_rag_or_capacity(tmp_path, status, expected):
    environment = _fixture_environment().model_copy(update={
        "run_mode": "real", "host": "local-test", "os": "Linux", "kernel": "test-kernel",
        "cpu": "test-cpu", "gpu": "test-gpu", "gpu_driver": "test-driver",
        "docker_version": "test-docker", "compose_version": "test-compose", "code_ref": "local-test",
        "model_runtime_version": "vllm-0.10.2",
        "resource_collection": "not-collected",
    })
    environment_path = tmp_path / "real-environment.json"
    environment_path.write_text(environment.model_dump_json(indent=2), encoding="utf-8")

    handler = type("LocalHandler", (_HealthHandler,), {"status": status})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        report = run_real_health(tmp_path / "run", base_url=f"http://127.0.0.1:{server.server_port}",
                                 environment_path=environment_path, paths=["/livez", "/readyz"],
                                 requests=3, concurrency=2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert report["status"] == expected
    assert report["coverage"] == "health-only"
    assert report["active_chunks"] is None
    assert report["registered_users"] is None
    assert report["query_concurrency"] is None
    assert report["health_concurrency"] == 2
    assert report["resource_samples"] == 0
    assert report["resource_collection"] == "not-collected"
    assert report["latency"].get("rag") is None
    assert "sensitive-response-body" not in (tmp_path / "run" / "samples.jsonl").read_text(encoding="utf-8")
    if status != 200:
        assert "PREFLIGHT_FAILURE_OBSERVED" in report["failures"]
