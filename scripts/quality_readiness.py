"""Check non-GPU T-064 readiness and refuse premature first-holdout execution."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.operations.eval_dataset import DatasetManifest, EvalCase, EvalLabel, validate_dataset  # noqa: E402
from app.operations.quality_eval import QualityLimits, QualityThresholds, wilson_interval  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def load_jsonl(path: Path, model: type[EvalCase] | type[EvalLabel]) -> list[EvalCase | EvalLabel]:
    return [model.model_validate_json(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def minimum_perfect_denominator(threshold: float, direction: Literal["min", "max"]) -> int:
    for total in range(1, 100_001):
        lower, upper = wilson_interval(total if direction == "min" else 0, total)
        if (direction == "min" and lower >= threshold) or (direction == "max" and upper <= threshold):
            return total
    raise ValueError("Threshold denominator search exceeded its safety bound")


def _selection_ready(path: Path | None, expected_status: str) -> tuple[bool, str | None]:
    if path is None:
        return False, None
    if path.is_symlink() or not path.is_file():
        raise ValueError("Selection evidence is missing or uses a symlink")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("Selection evidence must contain an object")
    return value.get("status") == expected_status, sha256_file(path)


def _confidence_preflight_blockers(
    requirements: dict[str, tuple[int | None, int]],
) -> list[str]:
    blockers: list[str] = []
    for name, (available, required) in requirements.items():
        # Output-dependent denominators, such as factual claims, do not exist
        # until the one permitted holdout run. They are evaluated after that
        # run and may yield insufficient-confidence, but cannot block it.
        if available is not None and available < required:
            blockers.append(f"{name.upper()}_WILSON_CONFIDENCE_INSUFFICIENT")
    return blockers


def check(
    dataset: Path,
    thresholds_path: Path,
    output: Path,
    *,
    retrieval_selection: Path | None = None,
    generation_selection: Path | None = None,
) -> dict[str, Any]:
    dataset = dataset.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Readiness output already exists; refusing to overwrite")
    if not output.parent.resolve().is_relative_to(dataset):
        raise ValueError("Quality readiness output must remain inside the private dataset")
    validation = validate_dataset(dataset, formal=True)
    manifest = DatasetManifest.model_validate_json((dataset / "dataset_manifest.json").read_text(encoding="utf-8-sig"))
    cases = [row for row in load_jsonl(dataset / manifest.cases_path, EvalCase) if isinstance(row, EvalCase)]
    labels = [row for row in load_jsonl(dataset / manifest.labels_path, EvalLabel) if isinstance(row, EvalLabel)]
    labels_by_id = {row.case_id: row for row in labels}
    seal_path = dataset / "holdout_seal.json"
    seal = json.loads(seal_path.read_text(encoding="utf-8-sig"))
    if seal.get("status") != "sealed" or seal.get("dataset_hash") != validation["dataset_hash"]:
        raise ValueError("Holdout seal does not match the validated dataset")
    thresholds = QualityThresholds.model_validate_json(thresholds_path.read_text(encoding="utf-8-sig"))
    if thresholds.dataset_hash != validation["dataset_hash"]:
        raise ValueError("Quality threshold dataset hash mismatch")
    holdout = [case for case in cases if case.split == "holdout"]
    answerable = [case for case in holdout if case.answerability == "answerable"]
    unanswerable = [case for case in holdout if case.answerability == "unanswerable"]
    high_answerable = [
        case for case in answerable if case.severity in {"high", "blocker"}
    ]
    high_facts = sum(len(labels_by_id[case.id].required_facts) for case in high_answerable)
    limits = thresholds.limits
    feasibility = {
        "strict_task_pass": {
            "available_denominator": len(holdout),
            "minimum_perfect_denominator": minimum_perfect_denominator(limits.strict_task_pass_min, "min"),
            "perfect_ci95": wilson_interval(len(holdout), len(holdout)),
        },
        "high_severity_fact_completeness": {
            "available_denominator": high_facts,
            "minimum_perfect_denominator": minimum_perfect_denominator(limits.high_severity_fact_completeness_min, "min"),
            "perfect_ci95": wilson_interval(high_facts, high_facts) if high_facts else None,
        },
        "claim_support": {
            "available_denominator": None,
            "minimum_perfect_denominator": minimum_perfect_denominator(limits.claim_support_min, "min"),
            "perfect_ci95": None,
        },
        "no_answer_recall": {
            "available_denominator": len(unanswerable),
            "minimum_perfect_denominator": minimum_perfect_denominator(limits.no_answer_recall_min, "min"),
            "perfect_ci95": wilson_interval(len(unanswerable), len(unanswerable)),
        },
        "false_refusal": {
            "available_denominator": len(answerable),
            "minimum_perfect_denominator": minimum_perfect_denominator(limits.false_refusal_max, "max"),
            "perfect_ci95": wilson_interval(0, len(answerable)),
        },
    }
    retrieval_ready, retrieval_hash = _selection_ready(retrieval_selection, "selected-dev-candidate")
    generation_ready, generation_hash = _selection_ready(generation_selection, "selected-dev-generation")
    blockers: list[str] = []
    if not retrieval_ready:
        blockers.append("T062_RETRIEVAL_SELECTION_NOT_COMPLETE")
    if not generation_ready:
        blockers.append("T063_GENERATION_SELECTION_NOT_COMPLETE")
    if thresholds.status != "frozen":
        blockers.append("QUALITY_THRESHOLDS_NOT_FROZEN")
    if seal.get("policy", {}).get("holdout_runs_completed") != 0:
        blockers.append("FIRST_HOLDOUT_RUN_ALREADY_CONSUMED")
    confidence_requirements: dict[str, tuple[int | None, int]] = {
        "strict_task_pass": (len(holdout), minimum_perfect_denominator(limits.strict_task_pass_min, "min")),
        "high_severity_fact_completeness": (
            high_facts,
            minimum_perfect_denominator(limits.high_severity_fact_completeness_min, "min"),
        ),
        "claim_support": (None, minimum_perfect_denominator(limits.claim_support_min, "min")),
        "no_answer_recall": (len(unanswerable), minimum_perfect_denominator(limits.no_answer_recall_min, "min")),
        "false_refusal": (len(answerable), minimum_perfect_denominator(limits.false_refusal_max, "max")),
    }
    blockers.extend(_confidence_preflight_blockers(confidence_requirements))
    runtime_only_confidence_checks = {
        name: {
            "status": "pending-first-run",
            "minimum_observed_denominator": required,
            "insufficient_result": "insufficient-confidence",
        }
        for name, (available, required) in confidence_requirements.items()
        if available is None
    }
    result = {
        "schema_version": 1,
        "status": "ready-for-first-holdout" if not blockers else "blocked",
        "scope": "t064-first-holdout-preflight",
        "formal": False,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "dataset_hash": validation["dataset_hash"],
        "holdout_seal_sha256": sha256_file(seal_path),
        "thresholds_sha256": sha256_file(thresholds_path),
        "threshold_status": thresholds.status,
        "retrieval_selection_sha256": retrieval_hash,
        "generation_selection_sha256": generation_hash,
        "holdout_runs_completed": seal["policy"]["holdout_runs_completed"],
        "holdout_counts": {
            "total": len(holdout),
            "answerable": len(answerable),
            "unanswerable": len(unanswerable),
            "high_or_blocker_answerable": len(high_answerable),
            "high_or_blocker_required_facts": high_facts,
        },
        "confidence_policy": "two-sided Wilson 95%; a point estimate alone cannot produce PASS",
        "confidence_feasibility": feasibility,
        "runtime_only_confidence_checks": runtime_only_confidence_checks,
        "blockers": sorted(blockers),
        "holdout_accessed_for_scoring": False,
        "decision": "This command is a metadata preflight only and never executes or scores holdout.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def write_draft(dataset: Path, output: Path) -> dict[str, Any]:
    dataset = dataset.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Threshold draft already exists; refusing to overwrite")
    if not output.parent.resolve().is_relative_to(dataset):
        raise ValueError("Threshold draft must remain inside the private dataset")
    validation = validate_dataset(dataset, formal=True)
    draft = QualityThresholds(
        schema_version=1,
        status="draft-not-frozen",
        dataset_hash=str(validation["dataset_hash"]),
        created_at=datetime.now(timezone.utc),
        limits=QualityLimits(),
    )
    value = draft.model_dump(mode="json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--thresholds", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--retrieval-selection", type=Path)
    parser.add_argument("--generation-selection", type=Path)
    parser.add_argument("--write-draft", action="store_true")
    args = parser.parse_args()
    try:
        if args.write_draft:
            if args.output is not None:
                parser.error("--write-draft uses --thresholds as its output; omit --output")
            result = write_draft(args.dataset, args.thresholds)
            print(json.dumps(result, ensure_ascii=False))
            return 0
        if args.output is None:
            parser.error("--output is required for readiness checks")
        result = check(
            args.dataset,
            args.thresholds,
            args.output,
            retrieval_selection=args.retrieval_selection,
            generation_selection=args.generation_selection,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "ready-for-first-holdout" else 2


if __name__ == "__main__":
    raise SystemExit(main())
