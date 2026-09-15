"""Prepare a private, dev-only T-063 human-review template without model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_retrieval_ablation import load_dev_view


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare(view: Path, output: Path) -> dict[str, object]:
    view = view.resolve()
    output = output.resolve()
    manifest_output = output.with_suffix(output.suffix + ".manifest.json")
    if output.exists() or manifest_output.exists():
        raise FileExistsError("Review template output already exists; refusing to overwrite")
    if output.suffix != ".jsonl" or not output.parent.resolve().is_relative_to(view):
        raise ValueError("Review template must be a JSONL file inside the private dev view")
    cases, labels, _corpus, view_manifest = load_dev_view(view)
    labels_by_id = {label.case_id: label for label in labels}
    rows = []
    for case in sorted(cases, key=lambda item: item.id):
        label = labels_by_id[case.id]
        rows.append(
            {
                "schema_version": 1,
                "record_status": "pending-review",
                "case_id": case.id,
                "split": "dev",
                "answerability": case.answerability,
                "severity": case.severity,
                "task_type": case.task_type,
                "required_facts_total": len(label.required_facts) if case.answerability == "answerable" else None,
                "system_output_sha256": None,
                "system_output_created_at": None,
                "system_status": None,
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
        )
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content, encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "status": "template-only",
        "scope": "t063-dev-generation-review",
        "formal": False,
        "formal_claim": "none",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset_hash": view_manifest["source_dataset_hash"],
        "dev_view_manifest_sha256": sha256_file(view / "dev_view_manifest.json"),
        "split": "dev",
        "case_count": len(rows),
        "holdout_rows": 0,
        "model_outputs_attached": 0,
        "completed_reviews": 0,
        "template_path": output.name,
        "template_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "next_gate": "Complete T-062 selection, generate dev outputs, then obtain two independent reviews and adjudicate disputes.",
    }
    manifest_output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-view", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = prepare(args.dev_view, args.output)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
