"""Transport-neutral public-search orchestration tests."""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scholight.api.models.search import PublicSearchRequest
from scholight.api.search_access import SearchQuotaReservation
from scholight.api.search_capacity import SearchCapacityError
from scholight.api.search_execution import (
    PublicSearchError,
    SearchInvocation,
    execute_public_search,
)
from scholight.models.search import SearchResult


def test_search_execution_has_no_transport_imports() -> None:
    module = Path(__file__).parents[1] / "search_execution.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    )

    assert imported.isdisjoint({"click", "fastapi", "mcp"})


@pytest.mark.asyncio
async def test_execute_public_search_uses_invocation_client_ip() -> None:
    reservation = SearchQuotaReservation(strength="standard")
    result = SearchResult(query="retrieval", level=1, total_ms=1.0, hits=[])

    with (
        patch(
            "scholight.api.search_execution.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ) as reserve,
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            return_value=result,
        ),
    ):
        response = await execute_public_search(
            PublicSearchRequest(query="retrieval"),
            SearchInvocation(
                actor=None,
                client_ip="192.0.2.20",
                request_id="request-1",
                transport="rest",
            ),
        )

    assert response.result_count == 0
    reserve.assert_awaited_once_with("192.0.2.20", None, strength="standard")


@pytest.mark.asyncio
async def test_capacity_rejection_happens_before_daily_quota_reservation() -> None:
    gate = MagicMock()

    @asynccontextmanager
    async def reject(_strength: str) -> AsyncIterator[None]:
        raise SearchCapacityError
        yield

    gate.admit = reject
    with (
        patch(
            "scholight.api.search_execution.enforce_search_pre_admission",
            new_callable=AsyncMock,
        ) as pre_admission,
        patch("scholight.api.search_execution.get_search_capacity_gate", return_value=gate),
        patch(
            "scholight.api.search_execution.reserve_search_quota",
            new_callable=AsyncMock,
        ) as reserve,
    ):
        with pytest.raises(PublicSearchError) as exc_info:
            await execute_public_search(
                PublicSearchRequest(query="retrieval"),
                SearchInvocation(
                    actor=None,
                    client_ip="192.0.2.20",
                    request_id="request-capacity",
                    transport="rest",
                ),
            )

    error = exc_info.value
    assert error.code == "search_capacity_exceeded"
    assert error.status_code == 503
    assert error.retry_after == 1
    pre_admission.assert_awaited_once()
    reserve.assert_not_awaited()
