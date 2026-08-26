from __future__ import annotations

import pytest

from scripts.check_survey_worker_update import IDLE_CONFIRMATION, select_worker_image


def _select(**overrides: object) -> str:
    values: dict[str, object] = {
        "current_image": "registry/survey@sha256:old",
        "desired_image": "registry/survey@sha256:new",
        "runtime_enabled": True,
        "metrics_observed": True,
        "draft_running": 0,
        "full_running": 0,
        "idle_confirmation": "",
        "require_idle": False,
    }
    values.update(overrides)
    return select_worker_image(**values)  # type: ignore[arg-type]


def test_unchanged_or_dormant_worker_image_is_safe() -> None:
    assert _select(desired_image="registry/survey@sha256:old", full_running=2).endswith("old")
    assert _select(runtime_enabled=False, full_running=2).endswith("new")
    assert _select(current_image="", full_running=2).endswith("new")


@pytest.mark.parametrize(("draft_running", "full_running"), [(1, 0), (0, 1), (3, 2)])
def test_active_work_always_retains_the_current_worker_image(
    draft_running: int,
    full_running: int,
) -> None:
    selected = _select(
        draft_running=draft_running,
        full_running=full_running,
        idle_confirmation=IDLE_CONFIRMATION,
    )
    assert selected.endswith("old")


def test_active_work_blocks_a_worker_resource_change() -> None:
    with pytest.raises(ValueError, match="resource change requires idle workers"):
        _select(require_idle=True, draft_running=1)


def test_resource_change_requires_observed_activity_metrics() -> None:
    with pytest.raises(ValueError, match="requires observed activity metrics"):
        _select(
            require_idle=True,
            metrics_observed=False,
            idle_confirmation=IDLE_CONFIRMATION,
        )


def test_missing_metrics_fail_closed_without_exact_operator_confirmation() -> None:
    with pytest.raises(ValueError, match="activity metrics are unavailable"):
        _select(metrics_observed=False)
    assert _select(metrics_observed=False, idle_confirmation=IDLE_CONFIRMATION).endswith("new")
