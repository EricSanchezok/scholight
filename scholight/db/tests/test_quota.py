"""Scholight-owned authenticated quota SQL contract tests."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from scholight.db.client import DBError
from scholight.db.queries_quota import (
    decrement_user_quota,
    get_user_quota_status,
    reserve_user_quota,
)
from scholight.models.quota import UserQuotaReservation


@pytest.mark.asyncio
async def test_reserve_uses_effective_override_and_never_increments_past_limit() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "daily_limit": 100,
            "quota_date": date(2026, 7, 23),
            "used_count": 1,
        }
    )

    with patch("scholight.db.queries_quota.get_pool", return_value=pool):
        reservation = await reserve_user_quota(
            42,
            strength="standard",
            default_limit=100,
        )

    assert reservation == UserQuotaReservation(
        user_id=42,
        strength="standard",
        quota_date=date(2026, 7, 23),
        used_count=1,
        daily_limit=100,
    )
    sql, *parameters = pool.fetchrow.await_args.args
    assert "scholight.user_quota_overrides" in sql
    assert "scholight.user_daily_search_usage" in sql
    assert "WHERE usage.used_count <" in sql
    assert parameters == [42, "standard", 100]


@pytest.mark.asyncio
async def test_reserve_returns_none_when_limit_is_zero_or_exhausted() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={"daily_limit": 0, "quota_date": None, "used_count": None}
    )

    with patch("scholight.db.queries_quota.get_pool", return_value=pool):
        reservation = await reserve_user_quota(
            42,
            strength="thorough",
            default_limit=0,
        )

    assert reservation is None


@pytest.mark.asyncio
async def test_decrement_uses_original_utc_date_and_product_identity() -> None:
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=9)
    reservation = UserQuotaReservation(
        user_id=42,
        strength="thorough",
        quota_date=date(2026, 7, 23),
        used_count=10,
        daily_limit=30,
    )

    with patch("scholight.db.queries_quota.get_pool", return_value=pool):
        decremented = await decrement_user_quota(reservation)

    assert decremented is True
    sql, *parameters = pool.fetchval.await_args.args
    assert "UPDATE scholight.user_daily_search_usage" in sql
    assert "AND used_count > 0" in sql
    assert parameters == [date(2026, 7, 23), 42, "thorough"]


@pytest.mark.asyncio
async def test_status_returns_both_product_strengths() -> None:
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {"strength": "standard", "daily_limit": 100, "used": 3, "remaining": 97},
            {"strength": "thorough", "daily_limit": 30, "used": 2, "remaining": 28},
        ]
    )

    with patch("scholight.db.queries_quota.get_pool", return_value=pool):
        statuses = await get_user_quota_status(
            42,
            standard_default_limit=100,
            thorough_default_limit=30,
        )

    assert [(status.strength, status.remaining) for status in statuses] == [
        ("standard", 97),
        ("thorough", 28),
    ]


@pytest.mark.asyncio
async def test_quota_queries_wrap_postgres_details() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(side_effect=asyncpg.PostgresError("private SQL detail"))

    with (
        patch("scholight.db.queries_quota.get_pool", return_value=pool),
        pytest.raises(DBError, match="user search quota") as exc_info,
    ):
        await reserve_user_quota(42, strength="standard", default_limit=100)

    assert "private SQL detail" not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strength", "limit"),
    [("invalid", 100), ("standard", -1), ("standard", True)],
)
async def test_reserve_rejects_invalid_input_before_database_access(
    strength: str, limit: int
) -> None:
    pool = MagicMock()

    with (
        patch("scholight.db.queries_quota.get_pool", return_value=pool),
        pytest.raises(ValueError),
    ):
        await reserve_user_quota(42, strength=strength, default_limit=limit)

    pool.fetchrow.assert_not_called()
