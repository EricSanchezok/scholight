"""Usage-event persistence and analytics SQL contract tests."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scholight.db.queries_usage import UsageEvent, insert_usage_event, query_latency


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
    ) -> bool | None:
        return None


@pytest.mark.asyncio
async def test_usage_insert_is_request_id_idempotent() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="INSERT 0 1")
    event = UsageEvent(
        request_id="request-1",
        user_id=42,
        operation="search_level1",
        strength="standard",
        actor_type="web",
        access_key_id=None,
        outcome="success",
        quota_units=1,
        result_count=10,
        search_duration_ms=800.0,
        status_code=200,
        error_code=None,
    )

    with patch("scholight.db.queries_usage.get_pool", return_value=pool):
        await insert_usage_event(event)

    sql = pool.execute.await_args.args[0]
    assert "ON CONFLICT (request_id) DO NOTHING" in sql
    assert "query_text" not in sql


@pytest.mark.asyncio
async def test_latency_query_uses_percentiles_and_excludes_failed() -> None:
    connection = MagicMock()
    connection.fetch = AsyncMock(return_value=[])
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(connection)
    start = datetime(2026, 7, 1, tzinfo=UTC)
    end = datetime(2026, 8, 1, tzinfo=UTC)

    with patch("scholight.db.queries_usage.get_pool", return_value=pool):
        await query_latency(42, start=start, end=end, access_key_id=None)

    sql = connection.fetch.await_args.args[0]
    assert "percentile_cont(0.5)" in sql
    assert "percentile_cont(0.95)" in sql
    assert "outcome IN ('success', 'degraded')" in sql
    assert "user_id = $1" in sql
