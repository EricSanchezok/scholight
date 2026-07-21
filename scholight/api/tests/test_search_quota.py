"""Search quota routing, compensation, and UTC pool tests."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cloud_auth.models.user import QuotaResult, UserRecord
from fastapi import HTTPException, Request

from scholight.api.search_access import compensate_search_quota, reserve_search_quota
from scholight.db import client as db_client
from scholight.db.queries_anonymous_quota import AnonymousQuotaReservation


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/search",
            "headers": [],
            "query_string": b"",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("192.0.2.20", 12345),
        }
    )


def _user() -> UserRecord:
    return UserRecord(
        id=42,
        email="active@example.com",
        password_hash="not-a-real-hash",
        status="active",
        email_verified=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("search_level", "limit"), [(1, 100), (2, 30)])
async def test_anonymous_search_routes_to_independent_daily_bucket(
    search_level: int, limit: int
) -> None:
    token = AnonymousQuotaReservation(
        quota_date=datetime(2026, 7, 21, tzinfo=UTC).date(),
        ip_digest=b"d" * 32,
        search_level=search_level,
        used_count=1,
    )

    with (
        patch(
            "scholight.api.search_access.reserve_anonymous_daily_quota",
            new_callable=AsyncMock,
            return_value=token,
        ) as reserve_anonymous,
        patch(
            "scholight.api.search_access.check_and_increment_quota",
            new_callable=AsyncMock,
        ) as reserve_user,
    ):
        reservation = await reserve_search_quota(_request(), None, search_level=search_level)

    assert reservation.anonymous is token
    assert reserve_anonymous.await_args.kwargs == {"search_level": search_level, "limit": limit}
    assert len(reserve_anonymous.await_args.args[0]) == 32
    reserve_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_anonymous_daily_limit_returns_stable_429() -> None:
    with patch(
        "scholight.api.search_access.reserve_anonymous_daily_quota",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(HTTPException) as exc_info:
            await reserve_search_quota(_request(), None, search_level=1)

    assert (exc_info.value.status_code, exc_info.value.detail) == (
        429,
        {
            "code": "anonymous_daily_limit_exceeded",
            "message": "Anonymous daily search limit exceeded.",
            "retryable": True,
        },
    )
    assert int(exc_info.value.headers["Retry-After"]) > 0


@pytest.mark.asyncio
async def test_authenticated_search_uses_only_cloud_auth_quota() -> None:
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    result = QuotaResult(allowed=True, current_count=1, daily_limit=1000)

    with (
        patch("scholight.api.search_access._utc_now", return_value=now),
        patch(
            "scholight.api.search_access.check_and_increment_quota",
            new_callable=AsyncMock,
            return_value=result,
        ) as reserve_user,
        patch(
            "scholight.api.search_access.reserve_anonymous_daily_quota",
            new_callable=AsyncMock,
        ) as reserve_anonymous,
    ):
        reservation = await reserve_search_quota(_request(), _user(), search_level=2)

    assert (
        reservation.operation,
        reservation.user_id,
        reservation.user_quota_date,
        reservation.user_quota_completed_date,
    ) == ("search_level2", 42, now.date(), now.date())
    reserve_user.assert_awaited_once()
    reserve_anonymous.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_compensation_is_one_shot_within_same_utc_day() -> None:
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    result = QuotaResult(allowed=True, current_count=1, daily_limit=1000)

    with (
        patch("scholight.api.search_access._utc_now", return_value=now),
        patch(
            "scholight.api.search_access.check_and_increment_quota",
            new_callable=AsyncMock,
            return_value=result,
        ),
        patch(
            "scholight.api.search_access.decrement_quota",
            new_callable=AsyncMock,
        ) as decrement,
    ):
        reservation = await reserve_search_quota(_request(), _user(), search_level=1)
        await compensate_search_quota(reservation)
        await compensate_search_quota(reservation)

    decrement.assert_awaited_once()


@pytest.mark.asyncio
async def test_user_quota_check_crossing_utc_midnight_never_decrements_new_day() -> None:
    before = datetime(2026, 7, 21, 23, 59, 59, tzinfo=UTC)
    after = datetime(2026, 7, 22, 0, 0, 1, tzinfo=UTC)
    result = QuotaResult(allowed=True, current_count=1, daily_limit=1000)

    with (
        patch("scholight.api.search_access._utc_now", side_effect=[before, after, after]),
        patch(
            "scholight.api.search_access.check_and_increment_quota",
            new_callable=AsyncMock,
            return_value=result,
        ),
        patch(
            "scholight.api.search_access.decrement_quota",
            new_callable=AsyncMock,
        ) as decrement,
        patch("scholight.api.search_access.logger.warning") as warning,
    ):
        reservation = await reserve_search_quota(_request(), _user(), search_level=1)
        await compensate_search_quota(reservation)

    assert (reservation.user_quota_date, reservation.user_quota_completed_date) == (
        before.date(),
        after.date(),
    )
    decrement.assert_not_awaited()
    warning.assert_called_once_with(
        "user_search_quota_compensation_skipped",
        reason="quota_check_crossed_utc_date",
    )


@pytest.mark.asyncio
async def test_postgres_pool_sets_every_session_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = MagicMock()
    create = AsyncMock(return_value=pool)
    monkeypatch.setattr(db_client, "_pool", None)
    monkeypatch.setattr(db_client.asyncpg, "create_pool", create)
    monkeypatch.setattr(db_client.settings, "pg_ssl_root_cert", "disable")

    created = await db_client.create_pool()
    db_client._pool = None

    assert created is pool
    assert create.await_args.kwargs["server_settings"] == {"TimeZone": "UTC"}
