"""Survey capacity telemetry and ECS task-protection contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from scholight.db.queries_survey_capacity import SurveyCapacitySnapshot
from scholight.survey.capacity import SurveyCapacityReporter, SurveyTaskProtection


@pytest.mark.asyncio
async def test_task_protection_is_a_safe_local_noop() -> None:
    protection = SurveyTaskProtection(service="survey-full-worker", agent_uri="")

    assert protection.enabled is False
    assert await protection.ensure() is True
    await protection.release()


@pytest.mark.asyncio
async def test_task_protection_enables_and_clears_ecs_protection() -> None:
    payloads: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        protection = SurveyTaskProtection(
            service="survey-full-worker",
            agent_uri="http://169.254.170.2/v1",
            client=client,
        )
        assert await protection.ensure() is True
        await protection.release()

    assert payloads == [
        {"ProtectionEnabled": True, "ExpiresInMinutes": 30},
        {"ProtectionEnabled": False},
    ]


@pytest.mark.asyncio
async def test_task_protection_refreshes_an_active_task() -> None:
    payloads: list[dict[str, object]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        protection = SurveyTaskProtection(
            service="survey-full-worker",
            agent_uri="http://169.254.170.2/v1",
            client=client,
        )
        assert await protection.ensure() is True
        protection._refresh_at = 0
        assert await protection.ensure() is True

    assert payloads == [
        {"ProtectionEnabled": True, "ExpiresInMinutes": 30},
        {"ProtectionEnabled": True, "ExpiresInMinutes": 30},
    ]


@pytest.mark.asyncio
async def test_task_protection_failure_stops_new_claims_and_emits_metric() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(503, request=request))
    async with httpx.AsyncClient(transport=transport) as client:
        protection = SurveyTaskProtection(
            service="survey-draft-worker",
            agent_uri="http://169.254.170.2/v1",
            client=client,
        )
        with patch("scholight.survey.capacity.emit_emf") as emit:
            assert await protection.ensure() is False

    emit.assert_called_once_with(
        service="survey-draft-worker",
        metrics={"SurveyTaskProtectionFailure": (1, "Count")},
    )


@pytest.mark.asyncio
async def test_capacity_reporter_emits_only_aggregate_metrics() -> None:
    snapshot = SurveyCapacitySnapshot(
        queued=10,
        running=6,
        outstanding=16,
        users_at_limit=2,
        oldest_queued_at=datetime.now(UTC) - timedelta(seconds=45),
    )
    reporter = SurveyCapacityReporter(
        queue="survey",
        service="survey-full-worker",
        per_user_concurrency=4,
    )
    with (
        patch(
            "scholight.survey.capacity.get_survey_capacity_snapshot",
            new=AsyncMock(return_value=snapshot),
        ),
        patch("scholight.survey.capacity.emit_emf") as emit,
    ):
        await reporter.emit_if_due()

    call = emit.call_args
    assert call.kwargs["service"] == "survey-full-worker"
    assert call.kwargs["metrics"]["SurveyJobQueued"] == (10, "Count")
    assert call.kwargs["metrics"]["SurveyJobRunning"] == (6, "Count")
    assert call.kwargs["metrics"]["SurveyJobOutstanding"] == (16, "Count")
    assert call.kwargs["metrics"]["SurveyJobUsersAtConcurrencyLimit"] == (2, "Count")
    assert set(call.kwargs) == {"service", "metrics"}
