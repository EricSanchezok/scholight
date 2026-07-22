"""Owner-scoped refresh-family session query tests."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scholight.db.queries_sessions import revoke_other_sessions, revoke_session, touch_session


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
async def test_revoke_session_is_owner_scoped() -> None:
    connection = MagicMock()
    connection.execute = AsyncMock(return_value="UPDATE 2")
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(connection)

    with patch("scholight.db.queries_sessions.get_pool", return_value=pool):
        revoked = await revoke_session(user_id=42, session_id=123)

    sql, user_id, session_id = connection.execute.await_args.args
    assert "user_id = $1 AND family_id = $2" in sql
    assert (user_id, session_id) == (42, 123)
    assert revoked


@pytest.mark.asyncio
async def test_revoke_others_preserves_current_family() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 3")

    with patch("scholight.db.queries_sessions.get_pool", return_value=pool):
        await revoke_other_sessions(user_id=42, current_session_id=123)

    sql, user_id, session_id = pool.execute.await_args.args
    assert "family_id <> $2" in sql
    assert (user_id, session_id) == (42, 123)


@pytest.mark.asyncio
async def test_revoked_family_fails_session_validation() -> None:
    pool = MagicMock()
    pool.fetchval = AsyncMock(return_value=False)
    pool.execute = AsyncMock()

    with patch("scholight.db.queries_sessions.get_pool", return_value=pool):
        active = await touch_session(user_id=42, session_id=123)

    assert not active
    pool.execute.assert_not_awaited()
