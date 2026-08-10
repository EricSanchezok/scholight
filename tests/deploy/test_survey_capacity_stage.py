from __future__ import annotations

import pytest

from scripts.check_survey_capacity_stage import validate_capacity_transition


@pytest.mark.parametrize(
    ("current", "desired"),
    [
        ((1, 1), (1, 1)),
        ((1, 1), (2, 2)),
        ((2, 2), (4, 4)),
        ((4, 4), (8, 8)),
        ((8, 8), (4, 4)),
    ],
)
def test_reviewed_capacity_transition_is_allowed(
    current: tuple[int, int],
    desired: tuple[int, int],
) -> None:
    validate_capacity_transition(
        current_draft=current[0],
        current_full=current[1],
        desired_draft=desired[0],
        desired_full=desired[1],
    )


@pytest.mark.parametrize(
    ("current", "desired"),
    [
        ((1, 1), (4, 4)),
        ((2, 2), (8, 8)),
        ((4, 4), (8, 16)),
        ((8, 16), (4, 4)),
        ((2, 1), (2, 2)),
        ((1, 1), (8, 4)),
    ],
)
def test_unreviewed_or_skipped_capacity_transition_is_rejected(
    current: tuple[int, int],
    desired: tuple[int, int],
) -> None:
    with pytest.raises(ValueError):
        validate_capacity_transition(
            current_draft=current[0],
            current_full=current[1],
            desired_draft=desired[0],
            desired_full=desired[1],
        )
