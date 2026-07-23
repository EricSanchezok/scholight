"""Strict Thorough API error and pre-commit compensation tests."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pymilvus.exceptions import MilvusException

from scholight.api.search_access import SearchQuotaReservation
from scholight.search.errors import SearchUnavailable, ThoroughSearchUnavailable


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
