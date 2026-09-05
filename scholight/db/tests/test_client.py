"""Database connection pinning contracts."""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from scholight.db import client as db_client


class _Acquire:
    def __init__(self, connection: object) -> None:
        self._connection = connection

    async def __aenter__(self) -> object:
        return self._connection

    async def __aexit__(self, *args: object) -> None:
        return None


class _Pool:
    def __init__(self, connection: object) -> None:
        self._connection = connection
        self.acquire_count = 0

    def acquire(self) -> _Acquire:
        self.acquire_count += 1
        return _Acquire(self._connection)


@pytest.mark.asyncio
async def test_bind_pool_connection_reuses_one_session_for_pool_queries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = AsyncMock()
    connection.execute.return_value = "SELECT 1"
    connection.fetchval.return_value = 7
    pool = _Pool(connection)
    monkeypatch.setattr(db_client, "_pool", None)

    async with db_client.bind_pool_connection(cast(Any, pool)) as acquired:
        assert acquired is connection
        assert await db_client.get_pool().execute("SELECT 1") == "SELECT 1"
        assert await db_client.get_pool().fetchval("SELECT 7") == 7
        async with db_client.get_pool().acquire() as reacquired:
            assert reacquired is connection

    assert pool.acquire_count == 1
    connection.execute.assert_awaited_once_with("SELECT 1", timeout=None)
    connection.fetchval.assert_awaited_once_with("SELECT 7", column=0, timeout=None)
    with pytest.raises(db_client.DBError, match="not initialised"):
        db_client.get_pool()
