"""Owner-scoped access-key query tests."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from scholight.db.queries_access_keys import revoke_access_key, touch_access_key_last_used


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
async def test_revoke_access_key_is_owner_scoped() -> None:
    connection = MagicMock()
    connection.execute = AsyncMock(return_value="UPDATE 1")
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(connection)
    key_id = uuid4()

    with patch("scholight.db.queries_access_keys.get_pool", return_value=pool):
        revoked = await revoke_access_key(key_id, user_id=42)

    assert revoked
    sql, called_key_id, called_user_id = connection.execute.await_args.args
    assert "id = $1 AND user_id = $2" in sql
    assert (called_key_id, called_user_id) == (key_id, 42)


@pytest.mark.asyncio
async def test_last_used_update_is_owner_scoped_and_throttled() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 1")
    key_id = uuid4()

    with patch("scholight.db.queries_access_keys.get_pool", return_value=pool):
        await touch_access_key_last_used(key_id, user_id=42)

    sql, called_key_id, called_user_id = pool.execute.await_args.args
    assert "id = $1 AND user_id = $2" in sql
    assert "interval '5 minutes'" in sql
    assert (called_key_id, called_user_id) == (key_id, 42)
