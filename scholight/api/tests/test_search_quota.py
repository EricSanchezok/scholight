"""Search quota routing, compensation, and UTC pool tests."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest
from cloud_auth.models.user import UserRecord

from scholight.api.search_access import (
    SearchAccessError,
    compensate_search_quota,
    reserve_search_quota,
)
from scholight.config import settings
from scholight.db import client as db_client
from scholight.db.queries_anonymous_quota import AnonymousQuotaReservation
from scholight.models.quota import UserQuotaReservation


def _user() -> UserRecord:
    return UserRecord(
        id=42,
        email="active@example.com",
        password_hash="not-a-real-hash",
        status="active",
        email_verified=True,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("strength", "limit"),
    [("standard", 100), ("thorough", 30)],
)
async def test_anonymous_search_routes_to_strength_bucket(strength: str, limit: int) -> None:
    token = AnonymousQuotaReservation(
        quota_date=date(2026, 7, 21),
        ip_digest=b"d" * 32,
        strength=strength,  # type: ignore[arg-type]
        used_count=1,
    )

    with (
        patch(
            "scholight.api.search_access.reserve_anonymous_daily_quota",
            new_callable=AsyncMock,
            return_value=token,
        ) as reserve_anonymous,
        patch(
            "scholight.api.search_access.reserve_user_quota",
            new_callable=AsyncMock,
        ) as reserve_user,
    ):
        reservation = await reserve_search_quota("192.0.2.20", None, strength=strength)

    assert reservation.anonymous is token
    anonymous_call = reserve_anonymous.await_args
    assert anonymous_call is not None
    assert anonymous_call.kwargs == {"strength": strength, "limit": limit}
    assert len(anonymous_call.args[0]) == 32
    reserve_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_anonymous_daily_limit_returns_stable_429() -> None:
    with patch(
        "scholight.api.search_access.reserve_anonymous_daily_quota",
        new_callable=AsyncMock,
        return_value=None,
    ):
        with pytest.raises(SearchAccessError) as exc_info:
            await reserve_search_quota("192.0.2.20", None, strength="standard")

    assert exc_info.value.status_code == 429
    assert exc_info.value.code == "anonymous_daily_limit_exceeded"
    assert exc_info.value.message == "Anonymous daily search limit exceeded."
    assert exc_info.value.retry_after > 0


@pytest.mark.asyncio
async def test_authenticated_search_uses_scholight_quota_only() -> None:
    token = UserQuotaReservation(
        user_id=42,
        strength="thorough",
        quota_date=date(2026, 7, 21),
        used_count=1,
        daily_limit=1000,
    )

    with (
        patch(
            "scholight.api.search_access.reserve_user_quota",
            new_callable=AsyncMock,
            return_value=token,
        ) as reserve_user,
        patch(
            "scholight.api.search_access.reserve_anonymous_daily_quota",
            new_callable=AsyncMock,
        ) as reserve_anonymous,
    ):
        reservation = await reserve_search_quota(None, _user(), strength="thorough")

    assert reservation.user is token
    reserve_user.assert_awaited_once_with(42, strength="thorough", default_limit=1000)
    reserve_anonymous.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_compensation_is_one_shot_for_exact_reservation_date() -> None:
    token = UserQuotaReservation(
        user_id=42,
        strength="standard",
        quota_date=date(2026, 7, 21),
        used_count=1,
        daily_limit=1000,
    )

    with (
        patch(
            "scholight.api.search_access.reserve_user_quota",
            new_callable=AsyncMock,
            return_value=token,
        ),
        patch(
            "scholight.api.search_access.decrement_user_quota",
            new_callable=AsyncMock,
        ) as decrement,
    ):
        reservation = await reserve_search_quota(None, _user(), strength="standard")
        await compensate_search_quota(reservation)
        await compensate_search_quota(reservation)

    decrement.assert_awaited_once_with(token)


@pytest.mark.asyncio
async def test_postgres_pool_sets_every_session_to_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = MagicMock()
    create = AsyncMock(return_value=pool)
    monkeypatch.setattr(db_client, "_pool", None)
    monkeypatch.setattr(asyncpg, "create_pool", create)
    monkeypatch.setattr(settings, "pg_ssl_root_cert", "disable")

    created = await db_client.create_pool()
    db_client._pool = None

    assert created is pool
    create_call = create.await_args
    assert create_call is not None
    assert create_call.kwargs["server_settings"] == {"TimeZone": "UTC"}
