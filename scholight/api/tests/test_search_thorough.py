"""Strict Thorough API error and pre-commit compensation tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pymilvus.exceptions import MilvusException

from scholight.api.search_access import SearchQuotaReservation
from scholight.api.search_capacity import SearchCapacityGate
from scholight.config import settings
from scholight.search.errors import SearchUnavailable, ThoroughSearchUnavailable


@pytest.mark.asyncio
async def test_capacity_rejection_returns_retry_after_without_reserving_daily_quota(
    api_client: httpx.AsyncClient,
) -> None:
    gate = SearchCapacityGate(total_limit=1, thorough_limit=1, wait_seconds=0)
    await gate.acquire("standard")
    try:
        with (
            patch("scholight.api.search_execution.get_search_capacity_gate", return_value=gate),
            patch(
                "scholight.api.search_execution.reserve_search_quota",
                new_callable=AsyncMock,
            ) as reserve,
        ):
            response = await api_client.post("/search", json={"query": "retrieval"})
    finally:
        gate.release("standard")

    assert (response.status_code, response.headers["retry-after"], response.json()) == (
        503,
        "1",
        {
            "detail": {
                "code": "search_capacity_exceeded",
                "message": "Search capacity is temporarily full.",
                "retryable": True,
            }
        },
    )
    reserve.assert_not_awaited()


@pytest.mark.asyncio
async def test_thorough_operational_failure_returns_503_and_compensates_once(
    api_client: httpx.AsyncClient,
) -> None:
    reservation = SearchQuotaReservation(strength="thorough")
    failure = ThoroughSearchUnavailable(
        phase_name="rrf_fusion",
        cause=MilvusException(message="private endpoint unavailable", code=1),
    )

    with (
        patch(
            "scholight.api.search_execution.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ),
        patch(
            "scholight.api.search_execution.compensate_search_quota",
            new_callable=AsyncMock,
        ) as compensate,
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            side_effect=failure,
        ),
        patch(
            "scholight.api.search_execution.schedule_search_history_write",
        ) as schedule_history,
    ):
        response = await api_client.post(
            "/search", json={"query": "retrieval", "strength": "thorough"}
        )

    assert (response.status_code, response.headers["retry-after"], response.json()) == (
        503,
        "5",
        {
            "detail": {
                "code": "thorough_search_unavailable",
                "message": "Thorough search is temporarily unavailable.",
                "retryable": True,
            }
        },
    )
    compensate.assert_awaited_once_with(reservation)
    schedule_history.assert_not_called()


@pytest.mark.asyncio
async def test_standard_operational_failure_uses_search_unavailable_code(
    api_client: httpx.AsyncClient,
) -> None:
    reservation = SearchQuotaReservation(strength="standard")
    failure = SearchUnavailable(
        phase_name="paper_search",
        cause=MilvusException(message="private endpoint unavailable", code=1),
    )

    with (
        patch(
            "scholight.api.search_execution.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ),
        patch(
            "scholight.api.search_execution.compensate_search_quota",
            new_callable=AsyncMock,
        ) as compensate,
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            side_effect=failure,
        ),
    ):
        response = await api_client.post("/search", json={"query": "retrieval"})

    assert (response.status_code, response.headers["retry-after"], response.json()) == (
        503,
        "5",
        {
            "detail": {
                "code": "search_unavailable",
                "message": "Search is temporarily unavailable.",
                "retryable": True,
            }
        },
    )
    compensate.assert_awaited_once_with(reservation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strength", "timeout", "code"),
    [
        ("standard", 0.01, "search_unavailable"),
        ("thorough", 0.01, "thorough_search_unavailable"),
    ],
)
async def test_total_search_timeout_returns_503_and_compensates_once(
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    strength: str,
    timeout: float,
    code: str,
) -> None:
    reservation = SearchQuotaReservation(strength=strength)  # type: ignore[arg-type]

    async def never_finishes(*_args: object) -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr(
        settings,
        (
            "search_standard_timeout_seconds"
            if strength == "standard"
            else "search_level2_timeout_seconds"
        ),
        timeout,
    )
    with (
        patch(
            "scholight.api.search_execution.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ),
        patch(
            "scholight.api.search_execution.compensate_search_quota",
            new_callable=AsyncMock,
        ) as compensate,
        patch(
            "scholight.search.engine.SearchEngine.search",
            new=never_finishes,
        ),
    ):
        response = await api_client.post(
            "/search",
            json={"query": "retrieval", "strength": strength},
        )

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == code
    compensate.assert_awaited_once_with(reservation)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [ValueError("program error"), asyncio.CancelledError()],
)
async def test_pre_commit_program_or_cancel_failure_returns_500_and_compensates_once(
    api_client: httpx.AsyncClient, failure: BaseException
) -> None:
    reservation = SearchQuotaReservation(strength="thorough")

    with (
        patch(
            "scholight.api.search_execution.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ),
        patch(
            "scholight.api.search_execution.compensate_search_quota",
            new_callable=AsyncMock,
        ) as compensate,
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            side_effect=failure,
        ),
        patch(
            "scholight.api.search_execution.schedule_search_history_write",
        ) as schedule_history,
    ):
        response = await api_client.post(
            "/search", json={"query": "retrieval", "strength": "thorough"}
        )

    assert (response.status_code, response.json()) == (
        500,
        {"detail": "Search service error"},
    )
    compensate.assert_awaited_once_with(reservation)
    schedule_history.assert_not_called()
