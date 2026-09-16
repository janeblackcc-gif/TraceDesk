"""Generate private, dev-only T-063 outputs from a frozen T-062 selection."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.models.vllm import VLLM  # noqa: E402
from app.providers import ModelUnavailable  # noqa: E402
from scripts.run_retrieval_ablation import build_candidates, load_dev_view  # noqa: E402


class GenerationProvider(Protocol):
    generation: str
    generation_digest: str

    def runtime_version(self) -> str: ...

    def generate(self, question: str, evidence: list[dict[str, Any]]) -> dict[str, object]: ...


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * percent) - 1)]


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def canonical_sha256(value: object) -> str:
    return sha256_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def pending_review_row(case: Any, required_facts_total: int | None, output: dict[str, Any]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_status": "pending-review",
        "case_id": case.id,
        "split": "dev",
        "answerability": case.answerability,
        "severity": case.severity,
        "task_type": case.task_type,
        "required_facts_total": required_facts_total,
        "system_output_sha256": output["system_output_sha256"],
        "system_output_created_at": output["created_at"],
        "system_status": output["system_status"],
        "strict_task_pass": None,
        "required_facts_satisfied": None,
        "factual_claims_total": None,
        "factual_claims_supported": None,
        "false_refusal": None,
        "correct_refusal": None,
        "citation_relevant": None,
        "scope_leakage": None,
        "version_leakage": None,
        "severe_error": None,
        "reviewer_ids": [],
        "dispute_status": None,
        "adjudicator_id": None,
        "failure_categories": [],
        "notes": "",
    }


def run(view: Path, retrieval_run: Path, output: Path, provider: GenerationProvider) -> dict[str, object]:
    view = view.resolve()
    retrieval_run = retrieval_run.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Generation output already exists; refusing to overwrite")
    if not retrieval_run.is_relative_to(view) or not output.is_relative_to(view):
        raise ValueError("Retrieval and generation outputs must remain inside the private dev view")

    cases, labels, corpus, view_manifest = load_dev_view(view)
    labels_by_id = {row.case_id: row for row in labels}
    selection_path = retrieval_run / "candidate-selection.json"
    status_path = retrieval_run / "status.json"
    config_path = retrieval_run / "config.json"
    rows_path = retrieval_run / "rows.private.jsonl"
    for path in (selection_path, status_path, config_path, rows_path):
        if path.is_symlink() or not path.is_file():
            raise ValueError("Frozen T-062 selection evidence is incomplete or uses a symlink")
    selection = load_json(selection_path)
    retrieval_status = load_json(status_path)
    retrieval_config = load_json(config_path)
    selected_variant = selection.get("selected_variant")
    if (
        selection.get("status") != "selected-dev-candidate"
        or selection.get("holdout_rows_loaded") != 0
        or selection.get("holdout_runs_completed") != 0
        or retrieval_status.get("status") != "completed-dev-retrieval"
        or retrieval_config.get("holdout_rows_loaded") != 0
        or retrieval_config.get("holdout_runs_completed") != 0
        or selected_variant != "bm25-single-no-adjacency"
    ):
        raise ValueError("T-063 requires the completed, dev-only frozen BM25 selection")
    if (
        sha256_file(status_path) != selection.get("source_status_sha256")
        or canonical_sha256(retrieval_config) != selection.get("source_config_sha256")
        or sha256_file(rows_path) != selection.get("source_rows_sha256")
    ):
        raise ValueError("Frozen T-062 evidence hash mismatch")
    if retrieval_status.get("dataset_hash") != view_manifest.get("source_dataset_hash"):
        raise ValueError("T-062 selection and dev-view dataset hashes differ")

    retrieval_rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    selected_rows = [row for row in retrieval_rows if row.get("variant") == selected_variant]
    rows_by_case = {row.get("case_id"): row for row in selected_rows}
    if len(rows_by_case) != len(cases) or set(rows_by_case) != {case.id for case in cases}:
        raise ValueError("Selected T-062 rows do not cover every dev case exactly once")
    candidates = build_candidates(view, corpus)
    candidates_by_id = {str(row["id"]): row for row in candidates}
    runtime_version = provider.runtime_version()

    output.mkdir(parents=True, exist_ok=False)
    raw_rows: list[dict[str, Any]] = []
    review_rows: list[dict[str, object]] = []
    durations: list[float] = []
    for case in sorted(cases, key=lambda item: item.id):
        retrieval_row = rows_by_case[case.id]
        context_ids = retrieval_row.get("context_ids")
        if (
            not isinstance(context_ids, list)
            or (not context_ids and case.answerability != "unanswerable")
            or any(item not in candidates_by_id for item in context_ids)
        ):
            raise ValueError(f"Selected retrieval context is invalid for {case.id}")
        evidence = [candidates_by_id[str(identifier)] for identifier in context_ids]
        created_at = datetime.now(timezone.utc).isoformat()
        started = time.perf_counter()
        if not evidence:
            result = {
                "abstain": True,
                "claims": [],
                "generation_assessment": {
                    "strategy": "retrieval_gate",
                    "decision": "abstain",
                    "reason": "no_retrieved_evidence",
                },
            }
            system_status = "abstained"
        else:
            try:
                result = provider.generate(case.question, evidence)
            except ModelUnavailable:
                result = {"error": "model-unavailable"}
                system_status = "error"
            else:
                system_status = "abstained" if result.get("abstain") is True else "answered"
        generation_ms = round((time.perf_counter() - started) * 1000, 3)
        durations.append(generation_ms)
        system_hash = canonical_sha256(result)
        raw_row = {
            "schema_version": 1,
            "case_id": case.id,
            "split": "dev",
            "question": case.question,
            "selected_variant": selected_variant,
            "retrieval_context_ids": context_ids,
            "created_at": created_at,
            "generation_ms": generation_ms,
            "system_status": system_status,
            "system_output_sha256": system_hash,
            "system_output": result,
        }
        raw_rows.append(raw_row)
        label = labels_by_id[case.id]
        required_total = len(label.required_facts) if case.answerability == "answerable" else None
        review_rows.append(pending_review_row(case, required_total, raw_row))

    raw_content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in raw_rows).encode("utf-8")
    review_content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in review_rows).encode("utf-8")
    (output / "outputs.private.jsonl").write_bytes(raw_content)
    (output / "generation-review.template.jsonl").write_bytes(review_content)
    counts = {value: sum(row["system_status"] == value for row in raw_rows) for value in ("answered", "abstained", "error")}
    config = {
        "schema_version": 1,
        "scope": "t063-dev-only-generation",
        "formal_claim": "none",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_dataset_hash": view_manifest["source_dataset_hash"],
        "dev_view_manifest_sha256": sha256_file(view / "dev_view_manifest.json"),
        "split": "dev",
        "holdout_rows_loaded": 0,
        "holdout_runs_completed": 0,
        "retrieval_selection_sha256": sha256_file(selection_path),
        "retrieval_status_sha256": sha256_file(status_path),
        "selected_variant": selected_variant,
        "generation_provider": "vllm",
        "generation_model": provider.generation,
        "generation_model_digest": provider.generation_digest,
        "vllm_version": runtime_version,
        "temperature": 0,
        "seed": 42,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "code_sha256": {
            "app/providers.py": sha256_file(ROOT / "app/providers.py"),
            "app/models/vllm.py": sha256_file(ROOT / "app/models/vllm.py"),
            "scripts/run_generation_eval.py": sha256_file(ROOT / "scripts/run_generation_eval.py"),
        },
    }
    config_content = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    (output / "config.json").write_bytes(config_content)
    status = {
        "schema_version": 1,
        "status": "completed-dev-generation" if counts["error"] == 0 else "completed-dev-generation-with-errors",
        "formal": False,
        "formal_claim": "none",
        "scope": "t063-dev-only-generation",
        "dataset_hash": view_manifest["source_dataset_hash"],
        "cases": len(cases),
        "answerable_cases": sum(case.answerability == "answerable" for case in cases),
        "unanswerable_cases": sum(case.answerability == "unanswerable" for case in cases),
        "selected_variant": selected_variant,
        "system_status_counts": counts,
        "generation_p50_ms": percentile(durations, 0.50),
        "generation_p95_ms": percentile(durations, 0.95),
        "outputs_sha256": sha256_bytes(raw_content),
        "review_template_sha256": sha256_bytes(review_content),
        "config_sha256": sha256_bytes(config_content),
        "completed_reviews": 0,
        "next_gate": "Obtain two independent human reviews and adjudicate disputes before aggregating dev quality.",
        "not_run": {"holdout": "sealed; thresholds remain draft and holdout was not loaded"},
    }
    (output / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-view", type=Path, required=True)
    parser.add_argument("--retrieval-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings.load()
    if settings.model_provider != "vllm":
        parser.error("T-063 generation requires TRACEDESK_MODEL_PROVIDER=vllm")
    try:
        result = run(args.dev_view, args.retrieval_run, args.output, VLLM(settings))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "completed-dev-generation" else 2


if __name__ == "__main__":
    raise SystemExit(main())
