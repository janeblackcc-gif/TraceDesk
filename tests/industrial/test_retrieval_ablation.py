import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.operations.eval_dataset import validate_dataset
from scripts.materialize_eval_dev_view import materialize
from scripts.prepare_generation_review import prepare
from scripts.run_retrieval_ablation import run
from scripts.select_retrieval_candidate import select


class FakeEmbeddingProvider:
    embedding = "fixture-embedding"

    def model_key(self) -> str:
        return "fixture-embedding@" + "a" * 64

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "开发" in text or "dev" in text else [0.0, 1.0] for text in texts]


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def formal_fixture(root: Path) -> Path:
    documents = root / "documents"
    documents.mkdir(parents=True)
    dev_document = documents / "dev.md"
    holdout_document = documents / "holdout.md"
    dev_document.write_text("# 配置\n开发环境服务端口是 8088。\n", encoding="utf-8")
    holdout_document.write_text("# 配置\n留出环境服务端口是 9099。\n", encoding="utf-8")
    corpus = {
        "format_version": 1,
        "corpus_id": "fixture",
        "version": "1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "retention_policy_reference": "fixture-policy",
        "documents": [
            {
                "path": f"documents/{path.name}",
                "sha256": sha256(path),
                "size_bytes": path.stat().st_size,
                "owner": "fixture-owner",
                "authorization_reference": "fixture-authorization",
                "authorized_uses": ["evaluation"],
                "authorization_expires_at": None,
            }
            for path in (dev_document, holdout_document)
        ],
    }
    corpus_path = root / "corpus_manifest.json"
    write_json(corpus_path, corpus)
    cases = []
    labels = []
    for index in range(40):
        split = "dev" if index < 20 else "holdout"
        digest = sha256(dev_document if split == "dev" else holdout_document)
        quote = "开发环境服务端口是 8088。" if split == "dev" else "留出环境服务端口是 9099。"
        cases.append(
            {
                "id": f"Q{index:03d}",
                "split": split,
                "task_type": "configuration",
                "answerability": "answerable",
                "severity": "high",
                "source_group": f"{split}-source",
                "template_group": f"{split}-template-{index}",
                "question": f"{split} 环境服务端口是多少？编号 {index}",
            }
        )
        labels.append(
            {
                "case_id": f"Q{index:03d}",
                "required_facts": ["默认端口数值"],
                "evidence_groups": [[{
                    "document_sha256": digest,
                    "quote": quote,
                    "quote_sha256": hashlib.sha256(quote.encode("utf-8")).hexdigest(),
                }]],
                "forbidden_claims": [],
                "reviewer_ids": ["reviewer-a", "reviewer-b"],
                "dispute_status": "none",
                "adjudicator_id": None,
            }
        )
    cases_path = root / "cases.jsonl"
    labels_path = root / "labels.private.jsonl"
    cases_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in cases), encoding="utf-8")
    labels_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in labels), encoding="utf-8")
    manifest = {
        "format_version": 2,
        "dataset_id": "fixture-real",
        "name": "Fixture",
        "version": "1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "corpus_manifest_path": corpus_path.name,
        "corpus_manifest_sha256": sha256(corpus_path),
        "cases_path": cases_path.name,
        "cases_sha256": sha256(cases_path),
        "labels_path": labels_path.name,
        "labels_sha256": sha256(labels_path),
        "sealed_holdout": True,
        "split_policy": "source_and_template_group",
    }
    write_json(root / "dataset_manifest.json", manifest)
    report = validate_dataset(root, formal=True)
    write_json(
        root / "holdout_seal.json",
        {
            "status": "sealed",
            "dataset_hash": report["dataset_hash"],
            "policy": {"holdout_runs_completed": 0},
        },
    )
    return root


def test_materialized_dev_view_excludes_holdout_and_runs_sparse_ablation(tmp_path: Path) -> None:
    dataset = formal_fixture(tmp_path / "dataset")
    view = dataset / "dev-only"
    manifest = materialize(dataset, view)
    assert manifest["case_count"] == 20
    assert manifest["document_count"] == 1
    assert manifest["holdout_rows"] == 0
    assert not (view / "documents" / "holdout.md").exists()
    assert "holdout" not in (view / "cases.dev.jsonl").read_text(encoding="utf-8")

    status = run(view, view / "experiments" / "sparse")
    assert status["status"] == "completed-sparse-only"
    assert status["cases"] == 20
    assert status["gold_evidence_groups_resolved"] == 20
    assert all(item["hit_at_4"]["rate"] == 1 for item in status["variant_summaries"].values())
    persisted = (view / "experiments" / "sparse" / "rows.private.jsonl").read_text(encoding="utf-8")
    assert "环境服务端口是多少" not in persisted
    assert "开发环境服务端口是" not in persisted
    assert '"severity": "high"' in persisted

    selection = select(
        view / "experiments" / "sparse",
        view / "experiments" / "sparse" / "candidate-selection.json",
    )
    assert selection["status"] == "blocked"
    assert selection["selected_variant"] is None

    template = prepare(view, view / "reviews" / "generation-review.template.jsonl")
    assert template["status"] == "template-only"
    review_text = (view / "reviews" / "generation-review.template.jsonl").read_text(encoding="utf-8")
    assert "环境服务端口是多少" not in review_text
    assert "开发环境服务端口是" not in review_text

    model_status = run(view, view / "experiments" / "model", FakeEmbeddingProvider())
    assert model_status["status"] == "completed-dev-retrieval"
    assert set(model_status["variant_summaries"]) == {
        "bm25-single-no-adjacency",
        "dense-single-no-adjacency",
        "hybrid-single-no-adjacency",
        "hybrid-literal-multi-no-adjacency",
        "hybrid-literal-multi-with-adjacency",
    }
    assert model_status["not_run"].get("dense") is None
    assert model_status["not_run"].get("hybrid") is None
    selected = select(
        view / "experiments" / "model",
        view / "experiments" / "model" / "candidate-selection.json",
    )
    assert selected["status"] == "selected-dev-candidate"
    assert selected["selected_variant"] == "bm25-single-no-adjacency"


def test_runner_rejects_a_hash_valid_view_containing_a_holdout_case(tmp_path: Path) -> None:
    dataset = formal_fixture(tmp_path / "dataset")
    view = dataset / "dev-only"
    materialize(dataset, view)
    cases_path = view / "cases.dev.jsonl"
    rows = [json.loads(line) for line in cases_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["split"] = "holdout"
    cases_path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    manifest_path = view / "dev_view_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cases_sha256"] = sha256(cases_path)
    write_json(manifest_path, manifest)
    with pytest.raises(ValueError, match="dev-only boundary"):
        run(view, view / "experiments" / "invalid")
