"""Select a T-062 dev retrieval candidate without reading questions or gold text."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXPECTED_MODEL_VARIANTS = {
    "bm25-single-no-adjacency",
    "dense-single-no-adjacency",
    "hybrid-single-no-adjacency",
    "hybrid-literal-multi-no-adjacency",
    "hybrid-literal-multi-with-adjacency",
}
COMPLEXITY = {
    "bm25-single-no-adjacency": 0,
    "dense-single-no-adjacency": 1,
    "hybrid-single-no-adjacency": 2,
    "hybrid-literal-multi-no-adjacency": 3,
    "hybrid-literal-multi-with-adjacency": 4,
}


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain an object")
    return value


def _rate(summary: dict[str, Any], name: str) -> float:
    value = summary.get(name)
    if not isinstance(value, dict) or not isinstance(value.get("rate"), (int, float)):
        raise ValueError(f"Missing numeric {name}.rate")
    return float(value["rate"])


def select(run: Path, output: Path, *, minimum_delta: float = 0.03) -> dict[str, Any]:
    run = run.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Selection output already exists; refusing to overwrite")
    if not run.is_dir() or run.is_symlink():
        raise ValueError("Retrieval run must be a real directory")
    if not output.parent.resolve().is_relative_to(run):
        raise ValueError("Selection output must remain inside the private retrieval run")
    status_path = run / "status.json"
    config_path = run / "config.json"
    status = _json(status_path)
    config = _json(config_path)
    if status.get("scope") != "t062-dev-only-retrieval-ablation" or status.get("formal") is not False:
        raise ValueError("Input is not a non-formal T-062 dev-only run")
    if config.get("split") != "dev" or config.get("holdout_rows_loaded") != 0 or config.get("holdout_runs_completed") != 0:
        raise ValueError("T-062 selection cannot consume holdout data")
    config_digest = sha256_bytes(json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    if config_digest != status.get("config_sha256"):
        raise ValueError("T-062 config hash mismatch")
    if status.get("dataset_hash") != config.get("source_dataset_hash"):
        raise ValueError("T-062 dataset identity mismatch")
    rows_name = status.get("rows_path")
    if not isinstance(rows_name, str) or Path(rows_name).name != rows_name:
        raise ValueError("T-062 rows path must be a local filename")
    rows_path = run / rows_name
    if rows_path.is_symlink() or not rows_path.is_file() or sha256_file(rows_path) != status.get("rows_sha256"):
        raise ValueError("T-062 rows are missing or hash-invalid")
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not rows or any(row.get("case_id") is None or row.get("variant") is None for row in rows):
        raise ValueError("T-062 rows are empty or malformed")
    if len({row["case_id"] for row in rows}) != status.get("cases"):
        raise ValueError("T-062 row case count differs from status")
    summaries = status.get("variant_summaries")
    if not isinstance(summaries, dict) or set(summaries) != {row["variant"] for row in rows}:
        raise ValueError("T-062 variant summaries differ from rows")

    common = {
        "schema_version": 1,
        "scope": "t062-dev-retrieval-selection",
        "formal": False,
        "formal_claim": "none",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_hash": status["dataset_hash"],
        "source_status_sha256": sha256_file(status_path),
        "source_config_sha256": status["config_sha256"],
        "source_rows_sha256": status["rows_sha256"],
        "minimum_meaningful_recall_delta": minimum_delta,
        "holdout_rows_loaded": 0,
        "holdout_runs_completed": 0,
    }
    if status.get("status") != "completed-dev-retrieval" or set(summaries) != EXPECTED_MODEL_VARIANTS:
        result = {
            **common,
            "status": "blocked",
            "selected_variant": None,
            "provisional_sparse_baseline": "bm25-single-no-adjacency",
            "blockers": ["DENSE_HYBRID_DEV_ARMS_NOT_COMPLETED"],
            "decision": "Sparse-only evidence cannot freeze the T-062 retrieval candidate.",
        }
    else:
        if any(row.get("severity") not in {"low", "medium", "high", "blocker"} for row in rows):
            raise ValueError("Complete model runs must record sealed case severity")
        baseline_name = "bm25-single-no-adjacency"
        baseline = summaries[baseline_name]
        baseline_recall = _rate(baseline, "evidence_group_recall_at_4")
        high_misses = {
            name: sum(
                row["severity"] in {"high", "blocker"}
                and isinstance(row.get("metrics"), dict)
                and row["metrics"]["evidence_groups_hit_at_4"] < row["metrics"]["evidence_groups_total"]
                for row in rows
                if row["variant"] == name
            )
            for name in summaries
        }
        meaningful = [baseline_name]
        reasons: dict[str, list[str]] = {baseline_name: ["reference-baseline"]}
        for name, summary in summaries.items():
            if name == baseline_name:
                continue
            delta = _rate(summary, "evidence_group_recall_at_4") - baseline_recall
            arm_reasons = []
            if delta >= minimum_delta:
                arm_reasons.append("recall-delta-at-least-minimum")
            if high_misses[name] < high_misses[baseline_name]:
                arm_reasons.append("repairs-high-severity-miss")
            if arm_reasons:
                meaningful.append(name)
                reasons[name] = arm_reasons

        def rank(name: str) -> tuple[float, float, float, float, int]:
            summary = summaries[name]
            return (
                _rate(summary, "evidence_group_recall_at_4"),
                _rate(summary, "context_complete_rate"),
                _rate(summary, "context_precision_micro"),
                -float(summary["retrieval_p95_ms"]),
                -COMPLEXITY[name],
            )

        selected = max(meaningful, key=rank)
        result = {
            **common,
            "status": "selected-dev-candidate",
            "selected_variant": selected,
            "eligible_variants": meaningful,
            "eligibility_reasons": reasons,
            "high_severity_misses": high_misses,
            "selection_order": [
                "evidence_group_recall_at_4",
                "context_complete_rate",
                "context_precision_micro",
                "retrieval_p95_ms",
                "implementation_complexity",
            ],
            "decision": "Selected on dev only; this does not authorize or score holdout.",
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--minimum-delta", type=float, default=0.03)
    args = parser.parse_args()
    if not 0 <= args.minimum_delta <= 1:
        parser.error("--minimum-delta must be between 0 and 1")
    try:
        result = select(args.run, args.output, minimum_delta=args.minimum_delta)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "selected-dev-candidate" else 2


if __name__ == "__main__":
    raise SystemExit(main())
