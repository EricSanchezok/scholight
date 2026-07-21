"""Search-history SQL contract tests."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from scholight.db.client import DBError
from scholight.db.queries_history import bulk_soft_delete_search_entries, get_search_history


class _AsyncContext(AbstractAsyncContextManager[MagicMock]):
    def __init__(self, value: MagicMock) -> None:
        self.value = value

    async def __aenter__(self) -> MagicMock:
        return self.value

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def _pool_with(connection: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(connection)
    connection.transaction.return_value = _AsyncContext(connection)
    return pool


def _row(entry_id: int, *, level: int = 1) -> dict[str, object]:
    return {
        "id": entry_id,
        "query_text": "retrieval",
        "level": level,
        "strategy": None,
        "filters": None,
        "num_results": 3,
        "response_time_ms": 12.5,
        "created_at": datetime(2026, 7, 21, 10, tzinfo=UTC),
    }


@pytest.mark.asyncio
async def test_history_page_uses_one_readonly_repeatable_read_snapshot() -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(return_value={"total": 1, "legacy_level3_count": 0})
    connection.fetch = AsyncMock(return_value=[_row(9)])

    with patch("scholight.db.queries_history.get_pool", return_value=_pool_with(connection)):
        page = await get_search_history(42, limit=20, offset=0)

    assert (page.total, [item.id for item in page.items]) == (1, [9])
    connection.transaction.assert_called_once_with(isolation="repeatable_read", readonly=True)
    count_sql = connection.fetchrow.await_args.args[0]
    page_sql = connection.fetch.await_args.args[0]
    assert "count(*) FILTER (WHERE level IN (1, 2)) AS total" in count_sql
    assert "count(*) FILTER (WHERE level = 3) AS legacy_level3_count" in count_sql
    assert "level IN (1, 2)" in page_sql
    assert "ORDER BY created_at DESC, id DESC" in page_sql
    assert "ILIKE" not in count_sql + page_sql


@pytest.mark.asyncio
async def test_history_q_is_literal_and_uses_distinct_static_sql() -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(return_value={"total": 0, "legacy_level3_count": 0})
    connection.fetch = AsyncMock(return_value=[])

    with patch("scholight.db.queries_history.get_pool", return_value=_pool_with(connection)):
        await get_search_history(42, limit=10, offset=5, q="100%_match\\")

    count_sql, user_id, pattern = connection.fetchrow.await_args.args
    page_sql, page_user_id, page_pattern, limit, offset = connection.fetch.await_args.args
    assert (user_id, page_user_id, pattern, page_pattern, limit, offset) == (
        42,
        42,
        "%100\\%\\_match\\\\%",
        "%100\\%\\_match\\\\%",
        10,
        5,
    )
    assert "query_text ILIKE $2 ESCAPE '\\'" in count_sql
    assert "query_text ILIKE $2 ESCAPE '\\'" in page_sql


@pytest.mark.asyncio
async def test_legacy_level3_rows_are_excluded_and_warned_once() -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(return_value={"total": 1, "legacy_level3_count": 2})
    connection.fetch = AsyncMock(return_value=[_row(1)])

    with (
        patch("scholight.db.queries_history.get_pool", return_value=_pool_with(connection)),
        patch("scholight.db.queries_history.logger.warning") as warning,
    ):
        page = await get_search_history(42)

    assert (page.total, page.legacy_level3_count) == (1, 2)
    warning.assert_called_once_with(
        "legacy_search_history_excluded",
        user_id=42,
        count=2,
    )


@pytest.mark.asyncio
async def test_bulk_delete_is_one_owner_scoped_statement_and_counts_rows() -> None:
    connection = MagicMock()
    connection.fetch = AsyncMock(return_value=[{"id": 3}, {"id": 1}])

    with patch("scholight.db.queries_history.get_pool", return_value=_pool_with(connection)):
        deleted = await bulk_soft_delete_search_entries(42, [3, 1, 99])

    sql, user_id, ids = connection.fetch.await_args.args
    assert (deleted, user_id, ids) == (2, 42, [3, 1, 99])
    assert "UPDATE public.search_history" in sql
    assert "deleted_at IS NULL" in sql
    assert "id = ANY($2::bigint[])" in sql
    assert "RETURNING id" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["page", "bulk"])
async def test_history_queries_wrap_postgres_errors_without_private_details(operation: str) -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(side_effect=asyncpg.PostgresError("private SQL detail"))
    connection.fetch = AsyncMock(side_effect=asyncpg.PostgresError("private SQL detail"))

    with (
        patch("scholight.db.queries_history.get_pool", return_value=_pool_with(connection)),
        pytest.raises(DBError, match="search history") as exc_info,
    ):
        if operation == "page":
            await get_search_history(42)
        else:
            await bulk_soft_delete_search_entries(42, [1])

    assert "private SQL detail" not in str(exc_info.value)
