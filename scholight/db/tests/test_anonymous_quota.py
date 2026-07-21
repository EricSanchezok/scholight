"""Anonymous daily-search quota persistence tests."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import date
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from scholight.db.client import DBError
from scholight.db.queries_anonymous_quota import (
    AnonymousQuotaReservation,
    decrement_anonymous_daily_quota,
    reserve_anonymous_daily_quota,
)


class _Acquire(AbstractAsyncContextManager[MagicMock]):
    def __init__(self, connection: MagicMock) -> None:
        self.connection = connection

    async def __aenter__(self) -> MagicMock:
        return self.connection

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


def _pool_with(connection: MagicMock) -> MagicMock:
    pool = MagicMock()
    pool.acquire.return_value = _Acquire(connection)
    return pool


@pytest.mark.asyncio
async def test_reserve_uses_one_atomic_upsert_and_returns_token() -> None:
    digest = b"d" * 32
    connection = MagicMock()
    connection.fetchrow = AsyncMock(return_value={"quota_date": date(2026, 7, 21), "used_count": 1})

    with patch(
        "scholight.db.queries_anonymous_quota.get_pool", return_value=_pool_with(connection)
    ):
        reservation = await reserve_anonymous_daily_quota(digest, search_level=1, limit=100)

    assert reservation == AnonymousQuotaReservation(
        quota_date=date(2026, 7, 21),
        ip_digest=digest,
        search_level=1,
        used_count=1,
    )
    sql, *parameters = connection.fetchrow.await_args.args
    assert "INSERT INTO public.anonymous_daily_search_usage" in sql
    assert "ON CONFLICT (quota_date, ip_digest, search_level)" in sql
    assert "WHERE usage.used_count < $3::integer" in sql
    assert "RETURNING quota_date, used_count" in sql
    assert "SELECT used_count" not in sql
    assert parameters == [digest, 1, 100]


@pytest.mark.asyncio
async def test_reserve_returns_none_when_daily_limit_is_exhausted() -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(return_value=None)

    with patch(
        "scholight.db.queries_anonymous_quota.get_pool", return_value=_pool_with(connection)
    ):
        reservation = await reserve_anonymous_daily_quota(b"d" * 32, search_level=2, limit=30)

    assert reservation is None


@pytest.mark.asyncio
async def test_decrement_uses_reservation_original_date_and_identity() -> None:
    reservation = AnonymousQuotaReservation(
        quota_date=date(2026, 7, 21),
        ip_digest=b"d" * 32,
        search_level=2,
        used_count=10,
    )
    connection = MagicMock()
    connection.fetchval = AsyncMock(return_value=9)

    with patch(
        "scholight.db.queries_anonymous_quota.get_pool", return_value=_pool_with(connection)
    ):
        decremented = await decrement_anonymous_daily_quota(reservation)

    assert decremented is True
    sql, *parameters = connection.fetchval.await_args.args
    assert "used_count = used_count - 1" in sql
    assert "AND used_count > 0" in sql
    assert parameters == [date(2026, 7, 21), b"d" * 32, 2]


@pytest.mark.asyncio
async def test_decrement_returns_false_when_reservation_row_is_missing() -> None:
    reservation = AnonymousQuotaReservation(
        quota_date=date(2026, 7, 21),
        ip_digest=b"d" * 32,
        search_level=1,
        used_count=1,
    )
    connection = MagicMock()
    connection.fetchval = AsyncMock(return_value=None)

    with patch(
        "scholight.db.queries_anonymous_quota.get_pool", return_value=_pool_with(connection)
    ):
        decremented = await decrement_anonymous_daily_quota(reservation)

    assert decremented is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("digest", "search_level", "limit"),
    [(b"short", 1, 100), (b"d" * 32, 3, 100), (b"d" * 32, 1, 0)],
)
async def test_reserve_rejects_invalid_invariants_before_database_access(
    digest: bytes, search_level: int, limit: int
) -> None:
    get_pool = MagicMock()

    with (
        patch("scholight.db.queries_anonymous_quota.get_pool", get_pool),
        pytest.raises(ValueError),
    ):
        await reserve_anonymous_daily_quota(digest, search_level=search_level, limit=limit)

    get_pool.assert_not_called()


@pytest.mark.asyncio
async def test_reserve_wraps_postgres_error_without_sensitive_details() -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(side_effect=asyncpg.PostgresError("private SQL detail"))

    with (
        patch("scholight.db.queries_anonymous_quota.get_pool", return_value=_pool_with(connection)),
        pytest.raises(DBError, match="anonymous search quota") as exc_info,
    ):
        await reserve_anonymous_daily_quota(b"d" * 32, search_level=1, limit=100)

    assert "private SQL detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_decrement_wraps_postgres_error_without_retry() -> None:
    reservation = AnonymousQuotaReservation(
        quota_date=date(2026, 7, 21),
        ip_digest=b"d" * 32,
        search_level=1,
        used_count=1,
    )
    connection = MagicMock()
    connection.fetchval = AsyncMock(side_effect=asyncpg.PostgresError("private SQL detail"))

    with (
        patch("scholight.db.queries_anonymous_quota.get_pool", return_value=_pool_with(connection)),
        pytest.raises(DBError, match="anonymous search quota"),
    ):
        await decrement_anonymous_daily_quota(reservation)

    assert connection.fetchval.await_count == 1
