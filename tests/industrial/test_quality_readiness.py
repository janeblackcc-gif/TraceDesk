from scripts.quality_readiness import _confidence_preflight_blockers


def test_output_dependent_denominator_does_not_block_first_holdout() -> None:
    blockers = _confidence_preflight_blockers(
        {
            "strict_task_pass": (140, 22),
            "claim_support": (None, 73),
        }
    )

    assert blockers == []


def test_known_insufficient_denominator_blocks_first_holdout() -> None:
    blockers = _confidence_preflight_blockers(
        {
            "strict_task_pass": (12, 22),
            "claim_support": (None, 73),
        }
    )

    assert blockers == ["STRICT_TASK_PASS_WILSON_CONFIDENCE_INSUFFICIENT"]
