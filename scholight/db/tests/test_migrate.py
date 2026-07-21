"""Migration runner concurrency and transaction ownership tests."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scholight.db.migrate import _MIGRATION_LOCK_ID, run_migrations


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


class _Transaction(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return None


@pytest.mark.asyncio
async def test_run_migrations_holds_advisory_lock_and_owns_transaction(tmp_path: Path) -> None:
    migration = tmp_path / "001_create_example.sql"
    migration.write_text("CREATE TABLE example (id INTEGER PRIMARY KEY);", encoding="utf-8")

    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=False)
    conn.transaction.return_value = _Transaction()
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(conn)

    with patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path):
        await run_migrations(pool)

    pool.acquire.assert_called_once_with()
    assert conn.execute.await_args_list[0].args == (
        "SELECT pg_advisory_lock($1)",
        _MIGRATION_LOCK_ID,
    )
    assert conn.execute.await_args_list[-1].args == (
        "SELECT pg_advisory_unlock($1)",
        _MIGRATION_LOCK_ID,
    )
    conn.transaction.assert_called_once_with()
    assert any(
        call.args == (migration.read_text(encoding="utf-8"),)
        for call in conn.execute.await_args_list
    )
