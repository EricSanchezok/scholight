"""Scholight-local product profile access tests."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import pytest

from scholight.db.client import DBError
from scholight.db.queries_profile import ProductAccessBlockedError, ensure_product_access


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


def _pool(status: object) -> tuple[MagicMock, MagicMock]:
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.fetchval = AsyncMock(return_value=status)
    connection.transaction.return_value = _Context(connection)
    pool = MagicMock()
    pool.acquire.return_value = _Context(connection)
    return pool, connection


@pytest.mark.asyncio
async def test_first_access_creates_active_product_profile_idempotently() -> None:
    pool, connection = _pool("active")

    with patch("scholight.db.queries_profile.get_pool", return_value=pool):
        await ensure_product_access(42)

    sql, user_id = connection.execute.await_args.args
    assert "INSERT INTO scholight.user_profiles" in sql
    assert "ON CONFLICT (user_id) DO NOTHING" in sql
    assert user_id == 42


@pytest.mark.asyncio
async def test_product_block_does_not_change_global_identity() -> None:
    pool, _ = _pool("blocked")

    with (
        patch("scholight.db.queries_profile.get_pool", return_value=pool),
        pytest.raises(ProductAccessBlockedError),
    ):
        await ensure_product_access(42)


@pytest.mark.asyncio
async def test_unknown_profile_state_fails_closed() -> None:
    pool, _ = _pool(None)

    with (
        patch("scholight.db.queries_profile.get_pool", return_value=pool),
        pytest.raises(DBError, match="Invalid Scholight product profile status"),
    ):
        await ensure_product_access(42)


@pytest.mark.asyncio
async def test_profile_query_wraps_postgres_details() -> None:
    pool, connection = _pool("active")
    connection.execute.side_effect = asyncpg.PostgresError("private SQL detail")

    with (
        patch("scholight.db.queries_profile.get_pool", return_value=pool),
        pytest.raises(DBError, match="Scholight product access") as exc_info,
    ):
        await ensure_product_access(42)

    assert "private SQL detail" not in str(exc_info.value)
