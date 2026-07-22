"""Account-deletion transaction ownership and cleanup tests."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scholight.db.queries_account import delete_user_account


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
async def test_account_cleanup_is_one_transaction_and_covers_all_credentials() -> None:
    connection = MagicMock()
    connection.execute = AsyncMock(return_value="UPDATE 1")
    connection.transaction.return_value = _AsyncContext(MagicMock())
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(connection)

    with patch("scholight.db.queries_account.get_pool", return_value=pool):
        await delete_user_account(42, replacement_password_hash="new-hash")

    sql = "\n".join(call.args[0] for call in connection.execute.await_args_list)
    assert "UPDATE auth.refresh_tokens" in sql
    assert "UPDATE public.access_keys" in sql
    assert "DELETE FROM public.search_history" in sql
    assert "DELETE FROM public.usage_events" in sql
    assert "UPDATE auth.users" in sql
    assert "status = 'disabled'" in sql
    assert all(42 in call.args[1:] for call in connection.execute.await_args_list)
