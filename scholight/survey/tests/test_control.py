"""Event-driven Survey control-plane contracts."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from scholight.db.client import DBError
from scholight.survey.control import SurveyControl, SurveyControlConfig


class _ECS:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run_task(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"tasks": [{"taskArn": "arn:task/one"}], "failures": []}

    def describe_tasks(self, **kwargs: object) -> dict[str, object]:
        return {"tasks": [], "failures": []}


def _config() -> SurveyControlConfig:
    return SurveyControlConfig(
        cluster_arn="arn:cluster",
        draft_task_definition_arn="arn:task-definition/draft:1",
        full_task_definition_arn="arn:task-definition/full:1",
        full_high_memory_task_definition_arn="arn:task-definition/full-high:1",
        subnet_ids=("subnet-a", "subnet-b"),
        security_group_id="sg-1",
        draft_global_concurrency=8,
        full_global_concurrency=2,
        full_per_user_concurrency=1,
    )


@pytest.mark.asyncio
async def test_launch_uses_attempt_as_ecs_idempotency_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_id = uuid4()
    attempt = SimpleNamespace(
        id=attempt_id,
        work_kind="full",
        draft_id=None,
        job_id=uuid4(),
        task_definition_arn="arn:task-definition/full:1",
        resource_profile="full-standard",
    )
    launching = AsyncMock(return_value=attempt)
    launched = AsyncMock()
    monkeypatch.setattr("scholight.survey.control.mark_compute_attempt_launching", launching)
    monkeypatch.setattr("scholight.survey.control.mark_compute_attempt_launched", launched)
    ecs = _ECS()
    control = SurveyControl(config=_config(), ecs_client=ecs)

    assert await control.launch_attempt(cast(Any, attempt))

    call = ecs.calls[0]
    assert call["clientToken"] == str(attempt_id)
    assert call["startedBy"] == str(attempt_id)
    assert call["taskDefinition"] == "arn:task-definition/full:1"
    overrides = call["overrides"]
    assert isinstance(overrides, dict)
    containers = overrides["containerOverrides"]
    assert isinstance(containers, list)
    assert containers[0]["command"] == [
        "scholight",
        "survey",
        "run-job",
        "--job-id",
        str(attempt.job_id),
        "--attempt-id",
        str(attempt_id),
    ]
    launched.assert_awaited_once_with(attempt_id=attempt_id, task_arn="arn:task/one")


@pytest.mark.asyncio
async def test_launch_retry_after_database_crash_reuses_the_same_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = SimpleNamespace(
        id=uuid4(),
        work_kind="draft",
        draft_id=uuid4(),
        job_id=None,
        task_definition_arn="arn:task-definition/draft:1",
        resource_profile="draft",
    )
    monkeypatch.setattr(
        "scholight.survey.control.mark_compute_attempt_launching",
        AsyncMock(return_value=attempt),
    )
    launched = AsyncMock(side_effect=[DBError("simulated crash"), attempt])
    monkeypatch.setattr("scholight.survey.control.mark_compute_attempt_launched", launched)
    ecs = _ECS()
    control = SurveyControl(config=_config(), ecs_client=ecs)

    with pytest.raises(DBError, match="simulated crash"):
        await control.launch_attempt(cast(Any, attempt))
    assert await control.launch_attempt(cast(Any, attempt))

    assert [call["clientToken"] for call in ecs.calls] == [str(attempt.id), str(attempt.id)]
    assert [call["startedBy"] for call in ecs.calls] == [str(attempt.id), str(attempt.id)]


@pytest.mark.asyncio
async def test_stopped_event_preserves_oom_and_allows_one_escalation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = SimpleNamespace(
        id=uuid4(),
        work_kind="full",
        attempt_no=1,
        resource_profile="full-standard",
        started_at=object(),
    )
    stopped = AsyncMock()
    monkeypatch.setattr(
        "scholight.survey.control.get_compute_attempt_by_task_arn",
        AsyncMock(return_value=attempt),
    )
    monkeypatch.setattr("scholight.survey.control.record_compute_attempt_stopped", stopped)
    monkeypatch.setattr(
        "scholight.survey.control.count_started_compute_attempts",
        AsyncMock(return_value=1),
    )
    control = SurveyControl(config=_config(), ecs_client=_ECS())
    event = {
        "detail": {
            "taskArn": "arn:task/one",
            "version": 12,
            "lastStatus": "STOPPED",
            "stopCode": "EssentialContainerExited",
            "stoppedReason": "Essential container exited",
            "containers": [
                {
                    "name": "survey",
                    "exitCode": 137,
                    "reason": "OutOfMemoryError: container killed",
                }
            ],
        }
    }

    assert await control.handle_stopped_event(event)

    assert stopped.await_args is not None
    kwargs = stopped.await_args.kwargs
    assert kwargs["failure_class"] == "oom"
    assert kwargs["retryable"] is True
    assert kwargs["failure_details"] == {"container_reason": "OutOfMemoryError"}


@pytest.mark.asyncio
async def test_high_memory_oom_settles_free_and_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = SimpleNamespace(
        id=uuid4(),
        job_id=uuid4(),
        work_kind="full",
        resource_profile="full-high-memory",
    )
    stopped = AsyncMock()
    settled = AsyncMock()
    monkeypatch.setattr(
        "scholight.survey.control.get_compute_attempt_by_task_arn",
        AsyncMock(return_value=attempt),
    )
    monkeypatch.setattr(
        "scholight.survey.control.count_started_compute_attempts", AsyncMock(return_value=2)
    )
    monkeypatch.setattr("scholight.survey.control.settle_survey_execution", settled)
    monkeypatch.setattr("scholight.survey.control.record_compute_attempt_stopped", stopped)
    control = SurveyControl(config=_config(), ecs_client=_ECS())

    handled = await control.handle_stopped_event(
        {
            "detail": {
                "taskArn": "arn:task/high",
                "version": 13,
                "lastStatus": "STOPPED",
                "containers": [{"name": "survey", "exitCode": 137}],
            }
        }
    )

    assert handled
    settled.assert_awaited_once_with(
        job_id=attempt.job_id,
        worker_id=attempt.id,
        outcome="failed",
        error_code="survey_compute_oom",
        error_message="Survey generation could not be completed.",
        chargeable=False,
    )
    assert stopped.await_args is not None
    assert stopped.await_args.kwargs["retryable"] is False
