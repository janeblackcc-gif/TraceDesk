from datetime import datetime, timedelta, timezone

from app.operations.eval_dataset import EvalCase
from app.operations.quality_eval import (
    GenerationReview,
    QualityThresholds,
    evaluate_generation_reviews,
    wilson_interval,
)


def thresholds(*, frozen: bool = True) -> QualityThresholds:
    now = datetime.now(timezone.utc)
    return QualityThresholds(
        schema_version=1,
        status="frozen" if frozen else "draft-not-frozen",
        dataset_hash="a" * 64,
        created_at=now - timedelta(minutes=2),
        frozen_at=now - timedelta(minutes=1) if frozen else None,
        retrieval_selection_sha256="b" * 64 if frozen else None,
        generation_selection_sha256="c" * 64 if frozen else None,
    )


def case(index: int, answerability: str) -> EvalCase:
    return EvalCase(
        id=f"Q{index:02d}",
        split="holdout",
        task_type="fixture",
        answerability=answerability,
        severity="high",
        source_group=f"source-{index}",
        template_group=f"template-{index}",
        question=f"fixture question {index}",
    )


def review(item: EvalCase) -> GenerationReview:
    answerable = item.answerability == "answerable"
    return GenerationReview(
        schema_version=1,
        record_status="completed-review",
        case_id=item.id,
        split=item.split,
        answerability=item.answerability,
        severity=item.severity,
        task_type=item.task_type,
        system_output_sha256="d" * 64,
        system_output_created_at=datetime.now(timezone.utc),
        system_status="answered" if answerable else "abstained",
        strict_task_pass=True,
        required_facts_total=1 if answerable else None,
        required_facts_satisfied=1 if answerable else None,
        factual_claims_total=1,
        factual_claims_supported=1,
        false_refusal=False if answerable else None,
        correct_refusal=None if answerable else True,
        citation_relevant=True,
        scope_leakage=False,
        version_leakage=False,
        severe_error=False,
        reviewer_ids=["reviewer-a", "reviewer-b"],
        dispute_status="none",
        adjudicator_id=None,
        failure_categories=[],
        notes="",
    )


def test_perfect_small_holdout_is_reported_as_insufficient_confidence() -> None:
    cases = [case(index, "answerable" if index < 9 else "unanswerable") for index in range(12)]
    report = evaluate_generation_reviews(cases, [review(item) for item in cases], thresholds(), expected_split="holdout")
    assert report["status"] == "insufficient-confidence"
    assert report["metrics"]["strict_task_pass"]["decision"] == "insufficient-confidence"
    assert report["metrics"]["no_answer_recall"]["denominator"] == 3


def test_holdout_scoring_rejects_unfrozen_thresholds() -> None:
    item = case(0, "answerable")
    try:
        evaluate_generation_reviews([item], [review(item)], thresholds(frozen=False), expected_split="holdout")
    except ValueError as exc:
        assert "thresholds frozen" in str(exc)
    else:
        raise AssertionError("Expected draft thresholds to block holdout scoring")


def test_wilson_interval_requires_more_than_twelve_perfect_cases_for_85_percent_lower_bound() -> None:
    lower, upper = wilson_interval(12, 12)
    assert lower < 0.85
    assert upper == 1.0
