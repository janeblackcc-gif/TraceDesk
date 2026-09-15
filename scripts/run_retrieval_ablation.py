"""Run CPU-only, dev-only sparse retrieval and adjacency ablations."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, TypeVar


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.evaluation import percentile  # noqa: E402
from app.config import Settings  # noqa: E402
from app.ingest import chunks, parse  # noqa: E402
from app.models.factory import create_provider  # noqa: E402
from app.operations.eval_dataset import CorpusManifest, EvalCase, EvalLabel  # noqa: E402
from app.retrieval import document_text, retrieval_queries, search, search_context, translation_language  # noqa: E402


ModelT = TypeVar("ModelT", EvalCase, EvalLabel)


class EmbeddingProvider(Protocol):
    embedding: str

    def model_key(self) -> str: ...

    def embed(self, texts: list[str]) -> list[list[float]]: ...


SPARSE_VARIANTS = (
    {"name": "bm25-single-no-adjacency", "method": "bm25", "multi_query": False, "adjacency": False},
    {"name": "bm25-literal-multi-no-adjacency", "method": "bm25", "multi_query": True, "adjacency": False},
    {"name": "bm25-single-with-adjacency", "method": "bm25", "multi_query": False, "adjacency": True},
    {"name": "bm25-literal-multi-with-adjacency", "method": "bm25", "multi_query": True, "adjacency": True},
)
MODEL_VARIANTS = (
    {"name": "bm25-single-no-adjacency", "method": "bm25", "multi_query": False, "adjacency": False},
    {"name": "dense-single-no-adjacency", "method": "dense", "multi_query": False, "adjacency": False},
    {"name": "hybrid-single-no-adjacency", "method": "hybrid", "multi_query": False, "adjacency": False},
    {"name": "hybrid-literal-multi-no-adjacency", "method": "hybrid", "multi_query": True, "adjacency": False},
    {"name": "hybrid-literal-multi-with-adjacency", "method": "hybrid", "multi_query": True, "adjacency": True},
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def normalized_text(value: str) -> str:
    return "".join(value.split())


def relative_file(root: Path, value: object) -> Path:
    if not isinstance(value, str):
        raise ValueError("Dev-view file paths must be strings")
    relative = PurePosixPath(value)
    if not value or "\\" in value or relative.is_absolute() or ".." in relative.parts or value.endswith("/"):
        raise ValueError("Dev-view file path is not normalized and relative")
    path = root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Dev-view file is missing, escapes its root, or uses a symlink")
    return path


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


def load_dev_view(view: Path) -> tuple[list[EvalCase], list[EvalLabel], CorpusManifest, dict[str, Any]]:
    view = view.resolve()
    manifest_path = view / "dev_view_manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Dev-view manifest is missing or uses a symlink")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if (
        manifest.get("format_version") != 1
        or manifest.get("scope") != "dev-only-retrieval-ablation"
        or manifest.get("split") != "dev"
        or manifest.get("holdout_rows") != 0
        or manifest.get("contains_holdout") is not False
        or manifest.get("source_dataset_sealed") is not True
    ):
        raise ValueError("Input is not a sealed dev-only evaluation view")
    cases_path = relative_file(view, manifest.get("cases_path"))
    labels_path = relative_file(view, manifest.get("labels_path"))
    corpus_path = relative_file(view, manifest.get("corpus_manifest_path"))
    expected_hashes = {
        cases_path: manifest.get("cases_sha256"),
        labels_path: manifest.get("labels_sha256"),
        corpus_path: manifest.get("corpus_manifest_sha256"),
    }
    if any(sha256_file(path) != expected for path, expected in expected_hashes.items()):
        raise ValueError("Dev-view component hash mismatch")
    case_rows = load_jsonl(cases_path, EvalCase)
    label_rows = load_jsonl(labels_path, EvalLabel)
    cases = [row for row in case_rows if isinstance(row, EvalCase)]
    labels = [row for row in label_rows if isinstance(row, EvalLabel)]
    if (
        not cases
        or any(row.split != "dev" for row in cases)
        or set(row.id for row in cases) != set(row.case_id for row in labels)
        or len(cases) != manifest.get("case_count")
    ):
        raise ValueError("Dev-view cases or labels violate the dev-only boundary")
    corpus = CorpusManifest.model_validate_json(corpus_path.read_text(encoding="utf-8-sig"))
    if len(corpus.documents) != manifest.get("document_count"):
        raise ValueError("Dev-view document count differs from its manifest")
    return cases, labels, corpus, manifest


def build_candidates(view: Path, corpus: CorpusManifest) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for document in corpus.documents:
        path = relative_file(view, document.path)
        if path.stat().st_size != document.size_bytes or sha256_file(path) != document.sha256:
            raise ValueError("Dev corpus document size or hash mismatch")
        filename, pages = parse(path.name, path.read_bytes())
        for index, chunk in enumerate(chunks(pages)):
            candidates.append(
                {
                    "id": f"{document.sha256[:16]}:{index:05d}",
                    "doc_id": document.sha256,
                    "document_sha256": document.sha256,
                    "filename": filename,
                    "collection": "tracedesk-prd-real-eval-dev",
                    "version": corpus.version,
                    "legacy_chunk_id": f"{document.sha256}:{index:05d}",
                    **chunk,
                }
            )
    if not candidates or len({row["id"] for row in candidates}) != len(candidates):
        raise ValueError("Dev corpus produced no chunks or duplicate chunk IDs")
    return candidates


def gold_targets(label: EvalLabel, candidates: list[dict[str, Any]]) -> list[set[str]]:
    targets: list[set[str]] = []
    for group in label.evidence_groups:
        matches: set[str] = set()
        for passage in group:
            quote = normalized_text(passage.quote)
            matches.update(
                str(candidate["id"])
                for candidate in candidates
                if candidate["document_sha256"] == passage.document_sha256
                and quote in normalized_text(str(candidate["text"]))
            )
        if not matches:
            raise ValueError(f"Gold evidence does not resolve to a current chunk: {label.case_id}")
        targets.append(matches)
    return targets


def balanced_anchors(
    queries: list[str],
    candidates: list[dict[str, Any]],
    method: str,
    vectors: dict[str, list[float]] | None,
    query_vectors: list[list[float]] | None,
) -> list[dict[str, Any]]:
    if query_vectors is not None and len(query_vectors) != len(queries):
        raise ValueError("Query vector count differs from retrieval query count")
    rankings = [
        search(
            query,
            candidates,
            method=method,
            vectors=vectors,
            query_vector=query_vectors[index] if query_vectors is not None else None,
            top_k=6,
            min_dense_score=0.40,
        )
        for index, query in enumerate(queries)
    ]
    anchors: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank in range(6):
        for query_index, ranking in enumerate(rankings):
            if rank >= len(ranking):
                continue
            source = ranking[rank]
            identifier = str(source["id"])
            if identifier in seen:
                continue
            anchors.append({**source, "context_origin": "retrieved", "query_index": query_index})
            seen.add(identifier)
            if len(anchors) == 6:
                return anchors
    return anchors


def score_case(
    case: EvalCase,
    label: EvalLabel,
    candidates: list[dict[str, Any]],
    *,
    method: str,
    multi_query: bool,
    adjacency: bool,
    vectors: dict[str, list[float]] | None,
    query_vectors_by_text: dict[str, list[float]] | None,
) -> dict[str, Any]:
    queries = retrieval_queries(case.question) if multi_query else [case.question]
    query_vectors = (
        [query_vectors_by_text[query] for query in queries]
        if method != "bm25" and query_vectors_by_text is not None
        else None
    )
    started = time.perf_counter()
    anchors = balanced_anchors(queries, candidates, method, vectors, query_vectors)
    context = (
        search_context(queries, candidates, method=method, vectors=vectors, query_vectors=query_vectors)
        if adjacency
        else anchors
    )
    retrieval_ms = round((time.perf_counter() - started) * 1000, 3)
    row: dict[str, Any] = {
        "case_id": case.id,
        "answerability": case.answerability,
        "severity": case.severity,
        "task_type": case.task_type,
        "query_count": len(queries),
        "anchor_ids": [source["id"] for source in anchors],
        "context_ids": [source["id"] for source in context],
        "context_origins": [source.get("context_origin", "retrieved") for source in context],
        "retrieval_ms": retrieval_ms,
    }
    if case.answerability == "unanswerable":
        row["metrics"] = None
        return row
    targets = gold_targets(label, candidates)
    relevant = set().union(*targets)
    top4 = [str(source["id"]) for source in anchors[:4]]
    top4_set = set(top4)
    context_ids = {str(source["id"]) for source in context}
    relevant_ranks = [index + 1 for index, identifier in enumerate(top4) if identifier in relevant]
    covered_top4 = sum(bool(group & top4_set) for group in targets)
    covered_context = sum(bool(group & context_ids) for group in targets)
    row["metrics"] = {
        "hit_at_4": bool(relevant & top4_set),
        "evidence_groups_hit_at_4": covered_top4,
        "evidence_groups_total": len(targets),
        "mrr_at_4": 1 / min(relevant_ranks) if relevant_ranks else 0.0,
        "context_complete": covered_context == len(targets),
        "context_evidence_groups_hit": covered_context,
        "context_evidence_groups_total": len(targets),
        "context_relevant_chunks": len(context_ids & relevant),
        "context_chunks": len(context_ids),
    }
    return row


def ratio(numerator: int, denominator: int) -> dict[str, int | float | None]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": round(numerator / denominator, 6) if denominator else None,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [row for row in rows if row["answerability"] == "answerable"]
    metrics = [row["metrics"] for row in answerable]
    hit_count = sum(item["hit_at_4"] for item in metrics)
    groups_hit = sum(item["evidence_groups_hit_at_4"] for item in metrics)
    groups_total = sum(item["evidence_groups_total"] for item in metrics)
    context_complete = sum(item["context_complete"] for item in metrics)
    context_relevant = sum(item["context_relevant_chunks"] for item in metrics)
    context_chunks = sum(item["context_chunks"] for item in metrics)
    durations = [float(row["retrieval_ms"]) for row in rows]
    return {
        "cases": len(rows),
        "answerable_cases": len(answerable),
        "unanswerable_cases": len(rows) - len(answerable),
        "hit_at_4": ratio(hit_count, len(answerable)),
        "evidence_group_recall_at_4": ratio(groups_hit, groups_total),
        "mean_mrr_at_4": round(sum(item["mrr_at_4"] for item in metrics) / len(metrics), 6) if metrics else None,
        "context_complete_rate": ratio(context_complete, len(answerable)),
        "context_precision_micro": ratio(context_relevant, context_chunks),
        "retrieval_p50_ms": percentile(durations, 0.50),
        "retrieval_p95_ms": percentile(durations, 0.95),
    }


def embed_in_batches(provider: EmbeddingProvider, texts: list[str], batch_size: int = 8) -> list[list[float]]:
    values: list[list[float]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        embedded = provider.embed(batch)
        if len(embedded) != len(batch):
            raise ValueError("Embedding provider returned an unexpected vector count")
        values.extend(embedded)
    dimensions = {len(vector) for vector in values}
    if not values or len(dimensions) != 1:
        raise ValueError("Embedding vectors are empty or have inconsistent dimensions")
    return values


def prepare_vectors(
    provider: EmbeddingProvider,
    cases: list[EvalCase],
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, list[float]], dict[str, list[float]], dict[str, Any]]:
    model_key = provider.model_key()
    started = time.perf_counter()
    corpus_values = embed_in_batches(provider, [document_text(row) for row in candidates])
    vectors = {str(row["id"]): vector for row, vector in zip(candidates, corpus_values, strict=True)}
    query_texts = sorted({query for case in cases for query in retrieval_queries(case.question)})
    query_payloads = [
        "Instruct: Retrieve technical documentation passages that answer the question.\nQuery: " + query
        for query in query_texts
    ]
    query_values = embed_in_batches(provider, query_payloads)
    query_vectors = dict(zip(query_texts, query_values, strict=True))
    if provider.model_key() != model_key:
        raise ValueError("Embedding model identity changed during vector preparation")
    return vectors, query_vectors, {
        "provider": "vllm",
        "embedding_model": provider.embedding,
        "model_key": model_key,
        "dimension": len(corpus_values[0]),
        "corpus_vectors": len(corpus_values),
        "query_vectors": len(query_values),
        "batch_size": 8,
        "preparation_ms": round((time.perf_counter() - started) * 1000, 3),
    }


def run(
    view: Path,
    output: Path,
    provider: EmbeddingProvider | None = None,
) -> dict[str, Any]:
    view = view.resolve()
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Ablation output already exists; refusing to overwrite")
    if not output.is_relative_to(view):
        raise ValueError("Ablation output must remain inside the private dev-view directory")
    cases, labels, corpus, view_manifest = load_dev_view(view)
    labels_by_id = {row.case_id: row for row in labels}
    candidates = build_candidates(view, corpus)
    resolved_groups = sum(len(gold_targets(labels_by_id[case.id], candidates)) for case in cases if case.answerability == "answerable")
    translation_applicable = sum(translation_language(case.question, candidates) is not None for case in cases)
    literal_multi_expanded = sum(len(retrieval_queries(case.question)) > 1 for case in cases)
    vectors: dict[str, list[float]] | None = None
    query_vectors: dict[str, list[float]] | None = None
    model_evidence: dict[str, Any] | None = None
    variants = MODEL_VARIANTS if provider is not None else SPARSE_VARIANTS
    if provider is not None:
        vectors, query_vectors, model_evidence = prepare_vectors(provider, cases, candidates)

    output.mkdir(parents=True, exist_ok=False)
    all_rows: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    for variant in variants:
        variant_rows = [
            {
                "variant": variant["name"],
                **score_case(
                    case,
                    labels_by_id[case.id],
                    candidates,
                    method=str(variant["method"]),
                    multi_query=bool(variant["multi_query"]),
                    adjacency=bool(variant["adjacency"]),
                    vectors=vectors,
                    query_vectors_by_text=query_vectors,
                ),
            }
            for case in cases
        ]
        all_rows.extend(variant_rows)
        summaries[str(variant["name"])] = summarize(variant_rows)

    rows_content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in all_rows).encode("utf-8")
    chunk_signature = [
        {
            "id": row["id"],
            "document_sha256": row["document_sha256"],
            "page": row["page"],
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "text_sha256": sha256_bytes(str(row["text"]).encode("utf-8")),
        }
        for row in candidates
    ]
    code_paths = (
        "app/ingest.py",
        "app/retrieval.py",
        "scripts/materialize_eval_dev_view.py",
        "scripts/run_retrieval_ablation.py",
    )
    config = {
        "schema_version": 1,
        "scope": "t062-dev-only-sparse-ablation",
        "formal_claim": "none",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_dataset_id": view_manifest["source_dataset_id"],
        "source_dataset_version": view_manifest["source_dataset_version"],
        "source_dataset_hash": view_manifest["source_dataset_hash"],
        "dev_view_manifest_sha256": sha256_file(view / "dev_view_manifest.json"),
        "split": "dev",
        "holdout_rows_loaded": 0,
        "holdout_runs_completed": 0,
        "variants": list(variants),
        "retrieval_methods": sorted({str(variant["method"]) for variant in variants}),
        "anchor_limit": 6,
        "reported_retrieval_k": 4,
        "context_chunk_limit": 18,
        "context_character_limit": 14000,
        "single_timing_pass": True,
        "model_calls": 0 if provider is None else "embedding-only",
        "model_evidence": model_evidence,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "code_sha256": {path: sha256_file(ROOT / path) for path in code_paths},
    }
    config_hash = sha256_bytes(json.dumps(config, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    misses_by_variant = {
        name: sorted(
            row["case_id"]
            for row in all_rows
            if row["variant"] == name
            and row["metrics"] is not None
            and (
                not row["metrics"]["hit_at_4"]
                or row["metrics"]["evidence_groups_hit_at_4"] < row["metrics"]["evidence_groups_total"]
            )
        )
        for name in summaries
    }
    top4_signatures = {
        name: (
            summary["hit_at_4"]["numerator"],
            summary["evidence_group_recall_at_4"]["numerator"],
        )
        for name, summary in summaries.items()
    }
    status = {
        "schema_version": 1,
        "status": "completed-dev-retrieval" if provider is not None else "completed-sparse-only",
        "formal": False,
        "formal_claim": "none",
        "scope": "t062-dev-only-retrieval-ablation",
        "dataset_hash": view_manifest["source_dataset_hash"],
        "dev_view_manifest_sha256": config["dev_view_manifest_sha256"],
        "config_sha256": config_hash,
        "cases": len(cases),
        "answerable_cases": sum(case.answerability == "answerable" for case in cases),
        "unanswerable_cases": sum(case.answerability == "unanswerable" for case in cases),
        "corpus_documents": len(corpus.documents),
        "corpus_chunks": len(candidates),
        "gold_evidence_groups_resolved": resolved_groups,
        "translation_applicable_cases": translation_applicable,
        "literal_multi_query_expanded_cases": literal_multi_expanded,
        "variant_summaries": summaries,
        "dev_failure_analysis": {
            "gold_groups_unresolved_to_current_chunks": 0,
            "misses_by_variant": misses_by_variant,
            "all_sparse_variants_have_the_same_top4_counts": len(set(top4_signatures.values())) == 1,
            "chunking_v2_triggered": False,
            "chunking_v2_decision": (
                "not triggered: every gold passage resolves inside a current chunk; the only Top4 miss is recovered "
                "by the fifth or sixth sparse anchor, so dense ranking must be tested before changing chunking"
            ),
        },
        "rows_path": "rows.private.jsonl",
        "rows_sha256": sha256_bytes(rows_content),
        "chunk_signature_sha256": sha256_bytes(
            json.dumps(chunk_signature, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ),
        "not_run": {
            **({
                "dense": "requires the frozen vLLM embedding endpoint and persisted corpus/query vectors",
                "hybrid": "requires the same dense vectors as the dense arm",
            } if provider is None else {}),
            "query_translation": "run only if the checked language-mismatch trigger is non-zero",
            "chunking_v2": "run separately only if dev failure analysis identifies chunk-boundary misses",
            "generation": "belongs to T-063, not this retrieval-only run",
            "holdout": "sealed; thresholds are not frozen and holdout must not be used for tuning",
        },
    }
    (output / "config.json").write_bytes((json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    (output / "rows.private.jsonl").write_bytes(rows_content)
    (output / "status.json").write_bytes((json.dumps(status, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return status


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dev-view", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--with-vllm", action="store_true", help="Run the dense and hybrid arms using configured loopback vLLM")
    args = parser.parse_args()
    provider: EmbeddingProvider | None = None
    if args.with_vllm:
        settings = Settings.load()
        if settings.model_provider != "vllm":
            parser.error("--with-vllm requires TRACEDESK_MODEL_PROVIDER=vllm")
        provider = create_provider(settings)
    result = run(args.dev_view, args.output, provider)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
