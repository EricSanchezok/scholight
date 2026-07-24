"""Product-scoped administration metrics query contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scholight.db.queries_admin_analytics import query_admin_analytics
from scholight.db.queries_admin_operations import query_admin_operations


@pytest.mark.asyncio
async def test_operations_queries_only_scholight_schema() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        side_effect=[
            {
                "last_successful_date": date(2026, 7, 23),
                "last_started_at": datetime(2026, 7, 24, tzinfo=UTC),
                "last_succeeded_at": datetime(2026, 7, 24, tzinfo=UTC),
                "last_error_code": None,
                "last_error_message": None,
            },
            {
                "pending": 2,
                "running": 1,
                "retry": 3,
                "succeeded": 10,
                "dead": 1,
                "oldest_waiting_at": datetime(2026, 7, 23, tzinfo=UTC),
            },
        ]
    )
    pool.fetch = AsyncMock(side_effect=[[], []])

    with patch("scholight.db.queries_admin_operations.get_pool", return_value=pool):
        await query_admin_operations(days=7, issue_limit=20)

    sql = " ".join(
        str(call.args[0]) for call in [*pool.fetchrow.await_args_list, *pool.fetch.await_args_list]
    ).lower()
    assert "scholight." in sql and "auth." not in sql and "public." not in sql


@pytest.mark.asyncio
async def test_analytics_queries_only_scholight_schema() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        side_effect=[
            {
                "total": 3,
                "active": 2,
                "blocked": 1,
                "admins": 1,
                "created_in_period": 1,
            },
            {
                "authenticated": 4,
                "standard": 3,
                "thorough": 1,
                "rest": 3,
                "mcp": 1,
                "success": 3,
                "degraded": 1,
                "failed": 0,
                "p50_response_ms": 100.0,
                "p95_response_ms": 200.0,
            },
            {"anonymous": 2, "standard": 2, "thorough": 0},
            {"total": 2, "active": 1, "used_in_period": 1},
        ]
    )
    pool.fetch = AsyncMock(return_value=[])

    with patch("scholight.db.queries_admin_analytics.get_pool", return_value=pool):
        await query_admin_analytics(
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 8, 1, tzinfo=UTC),
        )

    sql = " ".join(
        str(call.args[0]) for call in [*pool.fetchrow.await_args_list, *pool.fetch.await_args_list]
    ).lower()
    assert "scholight." in sql and "auth." not in sql and "public." not in sql


@pytest.mark.asyncio
async def test_analytics_combines_anonymous_and_authenticated_strength_totals() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        side_effect=[
            {
                "total": 3,
                "active": 2,
                "blocked": 1,
                "admins": 1,
                "created_in_period": 1,
            },
            {
                "authenticated": 4,
                "standard": 3,
                "thorough": 1,
                "rest": 3,
                "mcp": 1,
                "success": 3,
                "degraded": 1,
                "failed": 0,
                "p50_response_ms": 100.0,
                "p95_response_ms": 200.0,
            },
            {"anonymous": 2, "standard": 2, "thorough": 0},
            {"total": 2, "active": 1, "used_in_period": 1},
        ]
    )
    pool.fetch = AsyncMock(return_value=[])

    with patch("scholight.db.queries_admin_analytics.get_pool", return_value=pool):
        result = await query_admin_analytics(
            start=datetime(2026, 7, 1, tzinfo=UTC),
            end=datetime(2026, 8, 1, tzinfo=UTC),
        )

    assert result["searches"]["standard"] == 5
