"""Scholight quota-administrator query contracts."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import asyncpg
import pytest

from scholight.db.client import DBError
from scholight.db.queries_admin import (
    LastAdminError,
    TargetUserInactiveError,
    find_admin_target_by_email,
    grant_quota_admin,
    is_quota_admin,
    list_admin_audit_events,
    revoke_quota_admin,
    update_user_quota_overrides,
)


class _Context(AbstractAsyncContextManager[MagicMock]):
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


def _pool(connection: MagicMock) -> MagicMock:
    connection.transaction.return_value = _Context(connection)
    pool = MagicMock()
    pool.acquire.return_value = _Context(connection)
    return pool


@pytest.mark.asyncio
async def test_admin_check_requires_active_product_profile() -> None:
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=True)

    with patch("scholight.db.queries_admin.get_pool", return_value=pool):
        result = await is_quota_admin(42)

    sql = pool.fetchval.await_args.args[0]
    assert "status = 'active'" in sql
    assert "is_admin IS TRUE" in sql
    assert result is True


@pytest.mark.asyncio
async def test_admin_check_fails_closed_on_database_error() -> None:
    pool = MagicMock()
    pool.fetchval = AsyncMock(side_effect=asyncpg.PostgresError("private SQL detail"))

    with (
        patch("scholight.db.queries_admin.get_pool", return_value=pool),
        pytest.raises(DBError, match="administrator permission") as exc_info,
    ):
        await is_quota_admin(42)

    assert "private SQL detail" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_target_lookup_is_exact_and_case_insensitive() -> None:
    pool = MagicMock()
    pool.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "email": "Reader@Example.com",
            "display_name": "Reader",
            "account_status": "active",
            "email_verified": True,
        }
    )

    with patch("scholight.db.queries_admin.get_pool", return_value=pool):
        target = await find_admin_target_by_email("reader@example.com")

    sql, email = pool.fetchrow.await_args.args
    assert "lower(users.email) = lower($1)" in sql
    assert "LIKE" not in sql
    assert email == "reader@example.com"
    assert target is not None and target.id == 7


@pytest.mark.asyncio
async def test_quota_update_writes_both_strengths_and_audit_in_one_transaction() -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(
        side_effect=[
            {
                "id": 7,
                "email": "reader@example.com",
                "account_status": "active",
                "email_verified": True,
                "product_status": "active",
            },
        ]
    )
    connection.fetch = AsyncMock(
        side_effect=[
            [
                {"strength": "standard", "daily_limit": 100},
                {"strength": "thorough", "daily_limit": 20},
            ],
        ]
    )
    connection.execute = AsyncMock(return_value="INSERT 0 1")

    with patch("scholight.db.queries_admin.get_pool", return_value=_pool(connection)):
        changed = await update_user_quota_overrides(
            actor_user_id=42,
            actor_email="admin@example.com",
            target_user_id=7,
            standard=5000,
            thorough=None,
            event_id=UUID("00000000-0000-0000-0000-000000000001"),
        )

    assert changed is True
    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert any("user_quota_overrides" in sql and "standard" not in sql for sql in statements)
    assert any("DELETE FROM scholight.user_quota_overrides" in sql for sql in statements)
    assert any("INSERT INTO scholight.admin_audit_events" in sql for sql in statements)
    connection.transaction.assert_called_once_with()


@pytest.mark.asyncio
async def test_unchanged_quota_update_is_idempotent_without_audit() -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "email": "reader@example.com",
            "account_status": "active",
            "email_verified": True,
            "product_status": "active",
        }
    )
    connection.fetch = AsyncMock(
        return_value=[
            {"strength": "standard", "daily_limit": 5000},
            {"strength": "thorough", "daily_limit": None},
        ]
    )
    connection.execute = AsyncMock()

    with patch("scholight.db.queries_admin.get_pool", return_value=_pool(connection)):
        changed = await update_user_quota_overrides(
            actor_user_id=42,
            actor_email="admin@example.com",
            target_user_id=7,
            standard=5000,
            thorough=None,
            event_id=UUID("00000000-0000-0000-0000-000000000001"),
        )

    assert changed is False
    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert not any("admin_audit_events" in sql for sql in statements)
    assert not any("DELETE FROM scholight.user_quota_overrides" in sql for sql in statements)


@pytest.mark.asyncio
async def test_quota_update_rejects_inactive_target() -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "email": "reader@example.com",
            "account_status": "disabled",
            "email_verified": True,
            "product_status": "active",
        }
    )

    with (
        patch("scholight.db.queries_admin.get_pool", return_value=_pool(connection)),
        pytest.raises(TargetUserInactiveError),
    ):
        await update_user_quota_overrides(
            actor_user_id=42,
            actor_email="admin@example.com",
            target_user_id=7,
            standard=100,
            thorough=100,
            event_id=UUID("00000000-0000-0000-0000-000000000001"),
        )

    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert not any("UPDATE scholight.user_profiles" in sql for sql in statements)
    assert not any("admin_audit_events" in sql for sql in statements)


@pytest.mark.asyncio
async def test_admin_grant_requires_verified_active_identity_and_is_idempotent() -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "email": "admin@example.com",
            "account_status": "active",
            "email_verified": True,
            "product_status": None,
            "is_admin": False,
        }
    )
    connection.execute = AsyncMock()

    with patch("scholight.db.queries_admin.get_pool", return_value=_pool(connection)):
        changed = await grant_quota_admin(
            "ADMIN@example.com",
            event_id=UUID("00000000-0000-0000-0000-000000000001"),
        )

    assert changed is True
    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert any("INSERT INTO scholight.user_profiles" in sql for sql in statements)
    assert any(
        "admin_audit_events" in call.args[0] and "admin_granted" in call.args
        for call in connection.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_revoke_refuses_to_remove_last_active_admin() -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "email": "admin@example.com",
            "account_status": "active",
            "email_verified": True,
            "product_status": "active",
            "is_admin": True,
        }
    )
    connection.fetchval = AsyncMock(return_value=1)
    connection.execute = AsyncMock()

    with (
        patch("scholight.db.queries_admin.get_pool", return_value=_pool(connection)),
        pytest.raises(LastAdminError),
    ):
        await revoke_quota_admin(
            "admin@example.com",
            event_id=UUID("00000000-0000-0000-0000-000000000001"),
        )

    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert not any("UPDATE scholight.user_profiles" in sql for sql in statements)
    assert not any("admin_audit_events" in sql for sql in statements)


@pytest.mark.asyncio
async def test_revoke_inactive_admin_preserves_the_only_other_valid_admin() -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(
        return_value={
            "id": 7,
            "email": "disabled@example.com",
            "account_status": "disabled",
            "email_verified": True,
            "product_status": "active",
            "is_admin": True,
        }
    )
    connection.fetchval = AsyncMock(return_value=1)
    connection.execute = AsyncMock()

    with patch("scholight.db.queries_admin.get_pool", return_value=_pool(connection)):
        changed = await revoke_quota_admin(
            "disabled@example.com",
            event_id=UUID("00000000-0000-0000-0000-000000000001"),
        )

    assert changed is True
    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert any("UPDATE scholight.user_profiles" in sql for sql in statements)


@pytest.mark.asyncio
async def test_audit_list_decodes_default_asyncpg_json_strings() -> None:
    pool = MagicMock()
    pool.fetch = AsyncMock(
        return_value=[
            {
                "event_id": UUID("00000000-0000-0000-0000-000000000001"),
                "actor_type": "user",
                "actor_identifier": "admin@example.com",
                "target_user_id": 7,
                "target_email": "reader@example.com",
                "action": "quota_overrides_updated",
                "before_state": '{"standard":1000,"thorough":null}',
                "after_state": '{"standard":5000,"thorough":null}',
                "created_at": datetime(2026, 7, 23),
            }
        ]
    )

    with patch("scholight.db.queries_admin.get_pool", return_value=pool):
        events = await list_admin_audit_events(20)

    assert events[0].before_state == {"standard": 1000, "thorough": None}
