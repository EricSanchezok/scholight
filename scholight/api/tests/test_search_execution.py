"""Transport-neutral public-search orchestration tests."""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from scholight.api.models.search import PublicSearchRequest
from scholight.api.search_access import SearchQuotaReservation
from scholight.api.search_execution import SearchInvocation, execute_public_search
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
    reservation = SearchQuotaReservation(operation="search_level1")
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
            SearchInvocation(actor=None, client_ip="192.0.2.20", request_id="request-1"),
        )

    assert response.result_count == 0
    reserve.assert_awaited_once_with("192.0.2.20", None, search_level=1)
