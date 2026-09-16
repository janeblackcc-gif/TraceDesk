"""Execute the single sealed T-064 holdout generation run."""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.evaluation import percentile  # noqa: E402
from app.models.vllm import VLLM  # noqa: E402
from app.operations.eval_dataset import (  # noqa: E402
    CorpusManifest,
    DatasetManifest,
    EvalCase,
    EvalLabel,
    validate_dataset,
)
from app.operations.quality_eval import QualityThresholds  # noqa: E402
from app.providers import ModelUnavailable  # noqa: E402
from scripts.run_generation_eval import (  # noqa: E402
    GenerationProvider,
    canonical_sha256,
    sha256_file,
)
from scripts.run_retrieval_ablation import balanced_anchors, build_candidates, load_jsonl, relative_file  # noqa: E402


COMMIT = re.compile(r"^[0-9a-f]{40}$")
CLAIM_NAME = "first_holdout_run_claim.json"


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def real_file_inside(dataset: Path, path: Path) -> Path:
    resolved = path.resolve()
    if path.is_symlink() or not resolved.is_file() or not resolved.is_relative_to(dataset):
        raise ValueError("Formal holdout inputs must be real files inside the private dataset")
    return resolved


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def create_claim(path: Path, value: object) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        content = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def review_row(case: EvalCase, label: EvalLabel, output: dict[str, Any]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "record_status": "pending-review",
        "case_id": case.id,
        "split": "holdout",
        "answerability": case.answerability,
        "severity": case.severity,
        "task_type": case.task_type,
        "required_facts_total": len(label.required_facts) if case.answerability == "answerable" else None,
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


def load_holdout(
    dataset: Path,
    manifest: DatasetManifest,
    expected_cases: int,
    expected_source_groups: int,
) -> tuple[list[EvalCase], dict[str, EvalLabel], CorpusManifest, list[dict[str, Any]]]:
    cases_path = relative_file(dataset, manifest.cases_path)
    labels_path = relative_file(dataset, manifest.labels_path)
    corpus_path = relative_file(dataset, manifest.corpus_manifest_path)
    cases = [row for row in load_jsonl(cases_path, EvalCase) if row.split == "holdout"]
    labels = [row for row in load_jsonl(labels_path, EvalLabel) if row.case_id in {case.id for case in cases}]
    labels_by_id = {row.case_id: row for row in labels}
    if (
        len(cases) != expected_cases
        or len(labels_by_id) != expected_cases
        or len({case.id for case in cases}) != expected_cases
        or any(case.split != "holdout" for case in cases)
    ):
        raise ValueError("Holdout cases do not match the sealed first-run count")

    documents_by_group: dict[str, set[str]] = {}
    for case in cases:
        group = documents_by_group.setdefault(case.source_group, set())
        for evidence_group in labels_by_id[case.id].evidence_groups:
            group.update(passage.document_sha256 for passage in evidence_group)
    if (
        len(documents_by_group) != expected_source_groups
        or any(len(documents) != 1 for documents in documents_by_group.values())
    ):
        raise ValueError("Every holdout source group must resolve to exactly one authorized document")
    holdout_document_hashes = set().union(*documents_by_group.values())
    corpus = CorpusManifest.model_validate_json(corpus_path.read_text(encoding="utf-8-sig"))
    selected_documents = [document for document in corpus.documents if document.sha256 in holdout_document_hashes]
    if len(selected_documents) != len(holdout_document_hashes):
        raise ValueError("Holdout document scope differs from the sealed corpus")
    holdout_corpus = corpus.model_copy(update={"documents": selected_documents})
    candidates = build_candidates(dataset, holdout_corpus)
    return sorted(cases, key=lambda item: item.id), labels_by_id, holdout_corpus, candidates


def verify_code(selection: dict[str, Any], code_commit: str) -> dict[str, str]:
    if not COMMIT.fullmatch(code_commit) or selection.get("code_commit") != code_commit:
        raise ValueError("The executing code commit differs from the frozen generation selection")
    if selection.get("code_commit_verified") is not True:
        raise ValueError("The frozen generation selection is not bound to a verified commit")
    expected = selection.get("code_sha256")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("The frozen generation selection has no code hash manifest")
    verified: dict[str, str] = {}
    for relative, digest in expected.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise ValueError("The frozen code hash manifest is invalid")
        path = ROOT / relative
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(ROOT):
            raise ValueError("A frozen code path is missing, escapes the repository, or uses a symlink")
        observed = sha256_file(path)
        if observed != digest:
            raise ValueError(f"Frozen code hash mismatch: {relative}")
        verified[relative] = observed
    return verified


def run(
    dataset: Path,
    thresholds_path: Path,
    retrieval_selection_path: Path,
    generation_selection_path: Path,
    output: Path,
    provider: GenerationProvider,
    *,
    code_commit: str,
) -> dict[str, object]:
    dataset = dataset.resolve()
    output = output.resolve()
    if dataset.is_symlink() or not dataset.is_dir():
        raise ValueError("Private dataset root is missing or uses a symlink")
    if output.exists() or not output.is_relative_to(dataset):
        raise ValueError("Formal holdout output must be a new directory inside the private dataset")
    claim_path = dataset / CLAIM_NAME
    if claim_path.exists() or claim_path.is_symlink():
        raise FileExistsError("The first formal holdout run has already been claimed and cannot be repeated")

    thresholds_path = real_file_inside(dataset, thresholds_path)
    retrieval_selection_path = real_file_inside(dataset, retrieval_selection_path)
    generation_selection_path = real_file_inside(dataset, generation_selection_path)
    validation = validate_dataset(dataset, formal=True)
    dataset_hash = str(validation["dataset_hash"])
    manifest = DatasetManifest.model_validate_json((dataset / "dataset_manifest.json").read_text(encoding="utf-8-sig"))
    seal_path = real_file_inside(dataset, dataset / "holdout_seal.json")
    seal = load_json(seal_path)
    thresholds = QualityThresholds.model_validate_json(thresholds_path.read_text(encoding="utf-8-sig"))
    retrieval_selection = load_json(retrieval_selection_path)
    generation_selection = load_json(generation_selection_path)
    retrieval_hash = sha256_file(retrieval_selection_path)
    generation_hash = sha256_file(generation_selection_path)
    thresholds_hash = sha256_file(thresholds_path)
    if (
        seal.get("status") != "sealed"
        or seal.get("dataset_hash") != dataset_hash
        or seal.get("policy", {}).get("holdout_runs_completed") != 0
        or thresholds.status != "frozen"
        or thresholds.dataset_hash != dataset_hash
        or thresholds.retrieval_selection_sha256 != retrieval_hash
        or thresholds.generation_selection_sha256 != generation_hash
        or retrieval_selection.get("status") != "selected-dev-candidate"
        or retrieval_selection.get("dataset_hash") != dataset_hash
        or retrieval_selection.get("selected_variant") != "bm25-single-no-adjacency"
        or generation_selection.get("status") != "selected-dev-generation"
        or generation_selection.get("dataset_hash") != dataset_hash
        or generation_selection.get("holdout_rows_loaded") != 0
        or generation_selection.get("holdout_runs_completed") != 0
    ):
        raise ValueError("The sealed dataset, thresholds, or frozen selections are inconsistent")
    expected_cases = seal.get("holdout_cases")
    if not isinstance(expected_cases, int) or expected_cases < 1 or validation.get("holdout_cases") != expected_cases:
        raise ValueError("The holdout seal does not contain the validated case count")
    expected_source_groups = seal.get("holdout_source_groups")
    if not isinstance(expected_source_groups, int) or expected_source_groups < 1:
        raise ValueError("The holdout seal does not contain the source-group count")
    verified_code = verify_code(generation_selection, code_commit)
    if provider.generation != generation_selection.get("generation_model"):
        raise ValueError("The configured generation model differs from the frozen selection")
    if provider.generation_digest != generation_selection.get("generation_model_digest"):
        raise ValueError("The configured generation model digest differs from the frozen selection")
    runtime_version = provider.runtime_version()
    if runtime_version != generation_selection.get("vllm_version"):
        raise ValueError("The vLLM runtime version differs from the frozen selection")

    claimed_at = datetime.now(timezone.utc)
    if thresholds.frozen_at is None or claimed_at <= thresholds.frozen_at:
        raise ValueError("The first holdout run must start after threshold freeze")
    relative_output = output.relative_to(dataset).as_posix()
    claim = {
        "schema_version": 1,
        "status": "consumed-started",
        "scope": "t064-first-holdout-generation",
        "formal": True,
        "claimed_at": claimed_at.isoformat(),
        "dataset_hash": dataset_hash,
        "holdout_run_number": 1,
        "expected_cases": expected_cases,
        "intended_output": relative_output,
        "thresholds_sha256": thresholds_hash,
        "retrieval_selection_sha256": retrieval_hash,
        "generation_selection_sha256": generation_hash,
        "code_commit": code_commit,
        "rerun_allowed": False,
    }
    create_claim(claim_path, claim)

    try:
        output.mkdir(parents=True, exist_ok=False)
        cases, labels_by_id, corpus, candidates = load_holdout(
            dataset,
            manifest,
            expected_cases,
            expected_source_groups,
        )
        candidates_by_id = {str(row["id"]): row for row in candidates}
        started_at = datetime.now(timezone.utc)
        config = {
            "schema_version": 1,
            "scope": "t064-first-holdout-generation",
            "formal": True,
            "formal_claim": "generation-only",
            "created_at": started_at.isoformat(),
            "dataset_hash": dataset_hash,
            "holdout_run_number": 1,
            "holdout_cases": len(cases),
            "holdout_source_groups": len({case.source_group for case in cases}),
            "holdout_documents": len(corpus.documents),
            "corpus_chunks": len(candidates),
            "retrieval_selection_sha256": retrieval_hash,
            "generation_selection_sha256": generation_hash,
            "thresholds_sha256": thresholds_hash,
            "selected_variant": "bm25-single-no-adjacency",
            "generation_provider": "vllm",
            "generation_model": provider.generation,
            "generation_model_digest": provider.generation_digest,
            "vllm_version": runtime_version,
            "temperature": 0,
            "seed": 42,
            "retrieval_anchor_limit": 6,
            "retrieval_adjacency": False,
            "gold_quotes_or_required_facts_used_for_ranking": False,
            "document_scope_derived_from_sealed_source_groups": True,
            "code_commit": code_commit,
            "code_sha256": verified_code,
            "python": platform.python_version(),
            "platform": platform.platform(),
        }
        atomic_json(output / "config.json", config)
        durations: list[float] = []
        retrieval_durations: list[float] = []
        counts = {"answered": 0, "abstained": 0, "error": 0}
        outputs_path = output / "outputs.private.jsonl"
        reviews_path = output / "generation-review.template.jsonl"
        with outputs_path.open("x", encoding="utf-8", newline="\n") as outputs_stream, reviews_path.open(
            "x", encoding="utf-8", newline="\n"
        ) as reviews_stream:
            for index, case in enumerate(cases, start=1):
                retrieval_started = time.perf_counter()
                context = balanced_anchors([case.question], candidates, "bm25", None, None)
                retrieval_ms = round((time.perf_counter() - retrieval_started) * 1000, 3)
                retrieval_durations.append(retrieval_ms)
                context_ids = [str(row["id"]) for row in context]
                evidence = [candidates_by_id[identifier] for identifier in context_ids]
                created_at = datetime.now(timezone.utc).isoformat()
                generation_started = time.perf_counter()
                if not evidence:
                    result: dict[str, object] = {
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
                generation_ms = round((time.perf_counter() - generation_started) * 1000, 3)
                durations.append(generation_ms)
                counts[system_status] += 1
                raw_row = {
                    "schema_version": 1,
                    "case_id": case.id,
                    "split": "holdout",
                    "question": case.question,
                    "selected_variant": "bm25-single-no-adjacency",
                    "retrieval_context_ids": context_ids,
                    "retrieval_ms": retrieval_ms,
                    "created_at": created_at,
                    "generation_ms": generation_ms,
                    "system_status": system_status,
                    "system_output_sha256": canonical_sha256(result),
                    "system_output": result,
                }
                outputs_stream.write(json.dumps(raw_row, ensure_ascii=False) + "\n")
                reviews_stream.write(json.dumps(review_row(case, labels_by_id[case.id], raw_row), ensure_ascii=False) + "\n")
                outputs_stream.flush()
                reviews_stream.flush()
                os.fsync(outputs_stream.fileno())
                os.fsync(reviews_stream.fileno())
                atomic_json(
                    output / "progress.json",
                    {
                        "status": "running",
                        "completed_cases": index,
                        "expected_cases": len(cases),
                        "system_status_counts": counts,
                        "last_case_id": case.id,
                    },
                )

        completed_at = datetime.now(timezone.utc)
        status = {
            "schema_version": 1,
            "status": "completed-first-holdout-generation",
            "formal": True,
            "formal_claim": "generation-only",
            "scope": "t064-first-holdout-generation",
            "dataset_hash": dataset_hash,
            "holdout_run_number": 1,
            "holdout_runs_completed": 1,
            "cases": len(cases),
            "answerable_cases": sum(case.answerability == "answerable" for case in cases),
            "unanswerable_cases": sum(case.answerability == "unanswerable" for case in cases),
            "system_status_counts": counts,
            "retrieval_p50_ms": percentile(retrieval_durations, 0.50),
            "retrieval_p95_ms": percentile(retrieval_durations, 0.95),
            "generation_p50_ms": percentile(durations, 0.50),
            "generation_p95_ms": percentile(durations, 0.95),
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "outputs_sha256": sha256_file(outputs_path),
            "review_template_sha256": sha256_file(reviews_path),
            "config_sha256": sha256_file(output / "config.json"),
            "claim_sha256": sha256_file(claim_path),
            "completed_reviews": 0,
            "rerun_allowed": False,
            "next_gate": "Seal these outputs, obtain two independent human reviews, adjudicate disputes, and aggregate the first holdout quality report.",
        }
        atomic_json(output / "status.json", status)
        atomic_json(
            output / "progress.json",
            {
                "status": "completed",
                "completed_cases": len(cases),
                "expected_cases": len(cases),
                "system_status_counts": counts,
            },
        )
        return status
    except BaseException as exc:
        failure_path = output / "failure.json" if output.is_dir() else dataset / "first_holdout_run_failure.json"
        atomic_json(
            failure_path,
            {
                "schema_version": 1,
                "status": "failed-consumed-no-rerun",
                "scope": "t064-first-holdout-generation",
                "failed_at": datetime.now(timezone.utc).isoformat(),
                "error_type": type(exc).__name__,
                "holdout_run_number": 1,
                "rerun_allowed": False,
                "claim_sha256": sha256_file(claim_path),
            },
        )
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--thresholds", type=Path, required=True)
    parser.add_argument("--retrieval-selection", type=Path, required=True)
    parser.add_argument("--generation-selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--code-commit", required=True)
    args = parser.parse_args()
    settings = Settings.load()
    if settings.model_provider != "vllm":
        parser.error("The first formal holdout run requires TRACEDESK_MODEL_PROVIDER=vllm")
    try:
        result = run(
            args.dataset,
            args.thresholds,
            args.retrieval_selection,
            args.generation_selection,
            args.output,
            VLLM(settings),
            code_commit=args.code_commit,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "completed-first-holdout-generation" else 2


if __name__ == "__main__":
    raise SystemExit(main())
