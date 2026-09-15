"""Strict schemas and confidence-aware aggregation for private quality reviews."""
from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.operations.eval_dataset import EvalCase


SHA256 = r"^[0-9a-f]{64}$"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class QualityLimits(StrictModel):
    scope_leakage_max: int = Field(default=0, ge=0)
    version_leakage_max: int = Field(default=0, ge=0)
    severe_errors_max: int = Field(default=0, ge=0)
    strict_task_pass_min: float = Field(default=0.85, ge=0, le=1)
    high_severity_fact_completeness_min: float = Field(default=0.95, ge=0, le=1)
    claim_support_min: float = Field(default=0.95, ge=0, le=1)
    no_answer_recall_min: float = Field(default=0.90, ge=0, le=1)
    false_refusal_max: float = Field(default=0.10, ge=0, le=1)


class QualityThresholds(StrictModel):
    schema_version: Literal[1]
    status: Literal["draft-not-frozen", "frozen"]
    dataset_hash: str = Field(pattern=SHA256)
    created_at: datetime
    frozen_at: datetime | None = None
    confidence_level: float = Field(default=0.95, ge=0.95, le=0.95)
    retrieval_selection_sha256: str | None = Field(default=None, pattern=SHA256)
    generation_selection_sha256: str | None = Field(default=None, pattern=SHA256)
    limits: QualityLimits = Field(default_factory=QualityLimits)

    @model_validator(mode="after")
    def coherent(self) -> QualityThresholds:
        if self.created_at.utcoffset() is None:
            raise ValueError("Quality threshold timestamps must include a timezone")
        if self.status == "frozen":
            if self.frozen_at is None or self.frozen_at.utcoffset() is None:
                raise ValueError("Frozen thresholds require a timezone-aware frozen_at")
            if self.frozen_at < self.created_at:
                raise ValueError("frozen_at cannot precede created_at")
            if self.retrieval_selection_sha256 is None or self.generation_selection_sha256 is None:
                raise ValueError("Frozen thresholds require retrieval and generation selections")
        elif self.frozen_at is not None:
            raise ValueError("Draft thresholds cannot carry frozen_at")
        return self


class GenerationReview(StrictModel):
    schema_version: Literal[1]
    record_status: Literal["completed-review"]
    case_id: str = Field(min_length=1, max_length=200)
    split: Literal["dev", "holdout"]
    answerability: Literal["answerable", "unanswerable"]
    severity: Literal["low", "medium", "high", "blocker"]
    task_type: str = Field(min_length=1, max_length=100)
    system_output_sha256: str = Field(pattern=SHA256)
    system_output_created_at: datetime
    system_status: Literal["answered", "abstained", "error"]
    strict_task_pass: bool
    required_facts_total: int | None = Field(default=None, ge=0)
    required_facts_satisfied: int | None = Field(default=None, ge=0)
    factual_claims_total: int = Field(ge=0)
    factual_claims_supported: int = Field(ge=0)
    false_refusal: bool | None = None
    correct_refusal: bool | None = None
    citation_relevant: bool | None = None
    scope_leakage: bool
    version_leakage: bool
    severe_error: bool
    reviewer_ids: list[str] = Field(min_length=2)
    dispute_status: Literal["none", "unresolved", "resolved"]
    adjudicator_id: str | None = Field(default=None, max_length=200)
    failure_categories: list[str] = Field(default_factory=list)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def coherent(self) -> GenerationReview:
        if self.system_output_created_at.utcoffset() is None:
            raise ValueError("System output timestamp must include a timezone")
        if len(set(self.reviewer_ids)) != len(self.reviewer_ids) or any(not value.strip() for value in self.reviewer_ids):
            raise ValueError("Generation reviews require two distinct non-blank reviewer IDs")
        if len(set(self.failure_categories)) != len(self.failure_categories):
            raise ValueError("Failure categories must be unique")
        if self.factual_claims_supported > self.factual_claims_total:
            raise ValueError("Supported claims cannot exceed total claims")
        if self.answerability == "answerable":
            if self.required_facts_total is None or self.required_facts_total < 1:
                raise ValueError("Answerable cases require a positive required-fact denominator")
            if self.required_facts_satisfied is None or self.required_facts_satisfied > self.required_facts_total:
                raise ValueError("Answerable cases require a bounded required-fact numerator")
            if self.false_refusal is None or self.correct_refusal is not None:
                raise ValueError("Answerable cases require false_refusal only")
        else:
            if self.required_facts_total is not None or self.required_facts_satisfied is not None:
                raise ValueError("Unanswerable cases cannot carry required-fact scores")
            if self.correct_refusal is None or self.false_refusal is not None:
                raise ValueError("Unanswerable cases require correct_refusal only")
        if self.dispute_status == "unresolved":
            raise ValueError("Completed formal reviews cannot contain unresolved disputes")
        if self.dispute_status == "resolved" and not self.adjudicator_id:
            raise ValueError("Resolved disputes require an adjudicator")
        if self.dispute_status != "resolved" and self.adjudicator_id is not None:
            raise ValueError("Adjudicator is only valid for resolved disputes")
        return self


def wilson_interval(successes: int, total: int, *, z: float = 1.959963984540054) -> tuple[float, float]:
    if total <= 0 or successes < 0 or successes > total:
        raise ValueError("Wilson interval requires 0 <= successes <= total and total > 0")
    rate = successes / total
    denominator = 1 + z * z / total
    centre = (rate + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def _metric(successes: int, total: int, threshold: float, *, direction: Literal["min", "max"]) -> dict[str, object]:
    if total == 0:
        return {
            "numerator": successes,
            "denominator": total,
            "rate": None,
            "ci95": None,
            "threshold": threshold,
            "direction": direction,
            "decision": "insufficient-denominator",
        }
    lower, upper = wilson_interval(successes, total)
    rate = successes / total
    passed = lower >= threshold if direction == "min" else upper <= threshold
    point_passed = rate >= threshold if direction == "min" else rate <= threshold
    return {
        "numerator": successes,
        "denominator": total,
        "rate": round(rate, 6),
        "ci95": {"lower": round(lower, 6), "upper": round(upper, 6)},
        "threshold": threshold,
        "direction": direction,
        "decision": "passed" if passed else ("insufficient-confidence" if point_passed else "failed"),
    }


def evaluate_generation_reviews(
    cases: list[EvalCase],
    reviews: list[GenerationReview],
    thresholds: QualityThresholds,
    *,
    expected_split: Literal["dev", "holdout"],
) -> dict[str, object]:
    if thresholds.status != "frozen" and expected_split == "holdout":
        raise ValueError("Holdout scoring requires thresholds frozen before the run")
    if expected_split == "holdout" and (
        thresholds.frozen_at is None
        or any(row.system_output_created_at < thresholds.frozen_at for row in reviews)
    ):
        raise ValueError("Holdout outputs must be created after thresholds are frozen")
    selected_cases = {case.id: case for case in cases if case.split == expected_split}
    if not selected_cases or len(selected_cases) != len(reviews):
        raise ValueError("Review rows must cover the selected split exactly once")
    if len({row.case_id for row in reviews}) != len(reviews) or set(selected_cases) != {row.case_id for row in reviews}:
        raise ValueError("Review rows must map one-to-one to evaluation cases")
    for row in reviews:
        case = selected_cases[row.case_id]
        if (row.split, row.answerability, row.severity, row.task_type) != (
            case.split,
            case.answerability,
            case.severity,
            case.task_type,
        ):
            raise ValueError("Review metadata differs from the sealed case metadata")

    strict = sum(row.strict_task_pass for row in reviews)
    high_rows = [row for row in reviews if row.answerability == "answerable" and row.severity in {"high", "blocker"}]
    facts_total = sum(row.required_facts_total or 0 for row in high_rows)
    facts_satisfied = sum(row.required_facts_satisfied or 0 for row in high_rows)
    claims_total = sum(row.factual_claims_total for row in reviews)
    claims_supported = sum(row.factual_claims_supported for row in reviews)
    unanswerable = [row for row in reviews if row.answerability == "unanswerable"]
    answerable = [row for row in reviews if row.answerability == "answerable"]
    metrics = {
        "strict_task_pass": _metric(strict, len(reviews), thresholds.limits.strict_task_pass_min, direction="min"),
        "high_severity_fact_completeness": _metric(
            facts_satisfied, facts_total, thresholds.limits.high_severity_fact_completeness_min, direction="min"
        ),
        "claim_support": _metric(
            claims_supported, claims_total, thresholds.limits.claim_support_min, direction="min"
        ),
        "no_answer_recall": _metric(
            sum(bool(row.correct_refusal) for row in unanswerable),
            len(unanswerable),
            thresholds.limits.no_answer_recall_min,
            direction="min",
        ),
        "false_refusal": _metric(
            sum(bool(row.false_refusal) for row in answerable),
            len(answerable),
            thresholds.limits.false_refusal_max,
            direction="max",
        ),
    }
    event_counts = {
        "scope_leakage": sum(row.scope_leakage for row in reviews),
        "version_leakage": sum(row.version_leakage for row in reviews),
        "severe_errors": sum(row.severe_error for row in reviews),
    }
    event_passed = (
        event_counts["scope_leakage"] <= thresholds.limits.scope_leakage_max
        and event_counts["version_leakage"] <= thresholds.limits.version_leakage_max
        and event_counts["severe_errors"] <= thresholds.limits.severe_errors_max
    )
    decisions = [str(value["decision"]) for value in metrics.values()]
    status = "passed" if event_passed and all(value == "passed" for value in decisions) else "failed"
    if event_passed and "failed" not in decisions and any(value.startswith("insufficient") for value in decisions):
        status = "insufficient-confidence"
    return {
        "status": status,
        "formal": expected_split == "holdout",
        "split": expected_split,
        "dataset_role": "real_holdout" if expected_split == "holdout" else "real_dev",
        "thresholds_frozen_before_run": expected_split == "holdout" and thresholds.status == "frozen",
        "dataset_hash": thresholds.dataset_hash,
        "case_count": len(reviews),
        "metrics": metrics,
        "event_counts": event_counts,
        "scope_leaks": event_counts["scope_leakage"] + event_counts["version_leakage"],
        "severe_errors": event_counts["severe_errors"],
    }
