"""Validate completed private T-063/T-064 reviews and write an aggregate-only report."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, cast


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.operations.eval_dataset import DatasetManifest, EvalCase, validate_dataset  # noqa: E402
from app.operations.quality_eval import (  # noqa: E402
    GenerationReview,
    QualityThresholds,
    evaluate_generation_reviews,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def build_report(
    dataset: Path,
    reviews_path: Path,
    thresholds_path: Path,
    output: Path,
    *,
    split: Literal["dev", "holdout"],
) -> dict[str, object]:
    dataset = dataset.resolve()
    reviews_path = reviews_path.resolve()
    thresholds_path = thresholds_path.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Quality report already exists; refusing to overwrite")
    for path in (reviews_path, thresholds_path):
        if path.is_symlink() or not path.is_file() or not path.is_relative_to(dataset):
            raise ValueError("Quality inputs must be real files inside the private dataset")
    if not output.parent.resolve().is_relative_to(dataset):
        raise ValueError("Quality report must remain inside the private dataset")
    validation = validate_dataset(dataset, formal=True)
    manifest = DatasetManifest.model_validate_json((dataset / "dataset_manifest.json").read_text(encoding="utf-8-sig"))
    case_rows = [
        EvalCase.model_validate_json(line)
        for line in (dataset / manifest.cases_path).read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    reviews = [
        GenerationReview.model_validate_json(line)
        for line in reviews_path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip()
    ]
    if not reviews:
        raise ValueError("Completed review input cannot be empty")
    thresholds = QualityThresholds.model_validate_json(thresholds_path.read_text(encoding="utf-8-sig"))
    if thresholds.dataset_hash != validation["dataset_hash"]:
        raise ValueError("Thresholds do not match the validated dataset")
    if split == "holdout":
        seal = json.loads((dataset / "holdout_seal.json").read_text(encoding="utf-8-sig"))
        if seal.get("status") != "sealed" or seal.get("dataset_hash") != validation["dataset_hash"]:
            raise ValueError("Holdout seal does not match the validated dataset")
        if seal.get("policy", {}).get("holdout_runs_completed") != 0:
            raise ValueError("The sealed first-holdout run has already been consumed")
    report = evaluate_generation_reviews(case_rows, reviews, thresholds, expected_split=split)
    report.update(
        {
            "schema_version": 1,
            "scope": "t063-dev-quality" if split == "dev" else "t064-first-holdout-quality",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "reviews_sha256": sha256_file(reviews_path),
            "thresholds_sha256": sha256_file(thresholds_path),
            "contains_question_or_answer_text": False,
        }
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--thresholds", type=Path, required=True)
    parser.add_argument("--split", choices=("dev", "holdout"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = build_report(
            args.dataset,
            args.reviews,
            args.thresholds,
            args.report,
            split=cast(Literal["dev", "holdout"], args.split),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
