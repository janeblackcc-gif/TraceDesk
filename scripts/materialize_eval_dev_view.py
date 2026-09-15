"""Materialize a hash-bound dev-only view from a sealed formal dataset."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.operations.eval_dataset import (  # noqa: E402
    CorpusManifest,
    DatasetManifest,
    EvalCase,
    EvalLabel,
    validate_dataset,
)


ModelT = TypeVar("ModelT", EvalCase, EvalLabel)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_jsonl(path: Path, model: type[ModelT]) -> list[ModelT]:
    rows: list[ModelT] = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append(model.model_validate_json(line))
        except Exception as exc:
            raise ValueError(f"Invalid {path.name} row {number}") from exc
    return rows


def jsonl_bytes(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")


def json_bytes(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)


def document_hashes_by_source(
    cases: list[EvalCase], labels_by_id: dict[str, EvalLabel]
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for case in cases:
        hashes = result.setdefault(case.source_group, set())
        for group in labels_by_id[case.id].evidence_groups:
            hashes.update(passage.document_sha256 for passage in group)
    return result


def materialize(dataset: Path, output: Path) -> dict[str, Any]:
    dataset = dataset.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Dev-view output already exists; refusing to overwrite")
    if output == dataset or not output.is_relative_to(dataset):
        raise ValueError("Dev view must be a new child directory of the private dataset root")

    formal_report = validate_dataset(dataset, formal=True)
    manifest = DatasetManifest.model_validate_json(
        (dataset / "dataset_manifest.json").read_text(encoding="utf-8-sig")
    )
    seal_path = dataset / "holdout_seal.json"
    if not seal_path.is_file() or seal_path.is_symlink():
        raise ValueError("A real holdout seal is required before materializing a dev view")
    seal = json.loads(seal_path.read_text(encoding="utf-8-sig"))
    if (
        seal.get("status") != "sealed"
        or seal.get("dataset_hash") != formal_report["dataset_hash"]
        or seal.get("policy", {}).get("holdout_runs_completed") != 0
    ):
        raise ValueError("Holdout seal is missing, stale, or already records a run")

    corpus = CorpusManifest.model_validate_json(
        (dataset / manifest.corpus_manifest_path).read_text(encoding="utf-8-sig")
    )
    case_rows = load_jsonl(dataset / manifest.cases_path, EvalCase)
    label_rows = load_jsonl(dataset / manifest.labels_path, EvalLabel)
    cases = [row for row in case_rows if isinstance(row, EvalCase)]
    labels = [row for row in label_rows if isinstance(row, EvalLabel)]
    labels_by_id = {row.case_id: row for row in labels}
    if set(labels_by_id) != {row.id for row in cases}:
        raise ValueError("Formal cases and labels are not one-to-one")

    dev_cases = [row for row in cases if row.split == "dev"]
    holdout_cases = [row for row in cases if row.split == "holdout"]
    dev_sources = document_hashes_by_source(dev_cases, labels_by_id)
    holdout_sources = document_hashes_by_source(holdout_cases, labels_by_id)
    if any(len(hashes) != 1 for hashes in dev_sources.values()):
        raise ValueError("Every dev source group must resolve to exactly one corpus document")
    if any(len(hashes) != 1 for hashes in holdout_sources.values()):
        raise ValueError("Every holdout source group must resolve to exactly one corpus document")
    dev_hashes = set().union(*dev_sources.values())
    holdout_hashes = set().union(*holdout_sources.values())
    if dev_hashes & holdout_hashes:
        raise ValueError("A corpus document is shared by dev and holdout")
    corpus_by_hash = {row.sha256: row for row in corpus.documents}
    if dev_hashes | holdout_hashes != set(corpus_by_hash):
        raise ValueError("The source-group split does not cover the complete formal corpus")

    dev_labels = [labels_by_id[row.id] for row in dev_cases]
    if any(row.dispute_status == "unresolved" or len(row.reviewer_ids) < 2 for row in dev_labels):
        raise ValueError("Dev labels are not fully reviewed")
    case_content = jsonl_bytes([row.model_dump(mode="json") for row in dev_cases])
    label_content = jsonl_bytes([row.model_dump(mode="json") for row in dev_labels])

    output.mkdir(parents=True, exist_ok=False)
    documents_dir = output / "documents"
    documents_dir.mkdir()
    dev_documents: list[dict[str, Any]] = []
    for digest in sorted(dev_hashes):
        document = corpus_by_hash[digest]
        source = dataset / document.path
        destination = documents_dir / Path(document.path).name
        if source.is_symlink() or not source.is_file() or sha256_file(source) != digest:
            raise ValueError("A dev corpus document changed before isolation")
        shutil.copy2(source, destination)
        if sha256_file(destination) != digest:
            raise ValueError("A dev corpus document changed while copying")
        record = document.model_dump(mode="json")
        record["path"] = f"documents/{destination.name}"
        dev_documents.append(record)

    dev_corpus = {
        "format_version": 1,
        "corpus_id": f"{corpus.corpus_id}-dev-only",
        "version": f"{corpus.version}-dev-only",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "retention_policy_reference": corpus.retention_policy_reference,
        "documents": dev_documents,
    }
    corpus_content = json_bytes(dev_corpus)
    manifest_content = {
        "format_version": 1,
        "scope": "dev-only-retrieval-ablation",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_dataset_id": formal_report["dataset_id"],
        "source_dataset_version": formal_report["version"],
        "source_dataset_hash": formal_report["dataset_hash"],
        "source_dataset_sealed": True,
        "split": "dev",
        "holdout_rows": 0,
        "case_count": len(dev_cases),
        "document_count": len(dev_documents),
        "corpus_manifest_path": "corpus_manifest.dev.json",
        "corpus_manifest_sha256": sha256_bytes(corpus_content),
        "cases_path": "cases.dev.jsonl",
        "cases_sha256": sha256_bytes(case_content),
        "labels_path": "labels.dev.private.jsonl",
        "labels_sha256": sha256_bytes(label_content),
        "source_holdout_seal_sha256": sha256_file(seal_path),
        "authorized_use": "evaluation",
        "contains_holdout": False,
    }
    write_new(output / "corpus_manifest.dev.json", corpus_content)
    write_new(output / "cases.dev.jsonl", case_content)
    write_new(output / "labels.dev.private.jsonl", label_content)
    write_new(output / "dev_view_manifest.json", json_bytes(manifest_content))
    return manifest_content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = materialize(args.dataset, args.output)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
