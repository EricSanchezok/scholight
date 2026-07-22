"""Migration runner concurrency and transaction ownership tests."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from scholight.db.migrate import _MIGRATION_LOCK_ID, apply_migrations, run_migrations


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
    conn.fetchrow = AsyncMock(return_value=None)
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


@pytest.mark.asyncio
async def test_applied_migration_checksum_mismatch_fails_closed(tmp_path: Path) -> None:
    migration = tmp_path / "001_create_example.sql"
    migration.write_text("CREATE TABLE example (id INTEGER PRIMARY KEY);", encoding="utf-8")
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"name": "create_example", "checksum": "0" * 64})
    conn.transaction.return_value = _Transaction()
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(conn)

    with (
        patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path),
        pytest.raises(RuntimeError, match="checksum mismatch"),
    ):
        await run_migrations(pool)


@pytest.mark.asyncio
async def test_legacy_applied_migration_records_checksum_baseline(tmp_path: Path) -> None:
    migration = tmp_path / "001_create_example.sql"
    migration.write_text("CREATE TABLE example (id INTEGER PRIMARY KEY);", encoding="utf-8")
    conn = MagicMock()
    conn.execute = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"name": "create_example", "checksum": None})
    conn.transaction.return_value = _Transaction()
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(conn)

    with patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path):
        await run_migrations(pool)

    assert any(
        call.args[0] == "UPDATE _migrations SET checksum = $2 WHERE version = $1"
        for call in conn.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_unrecognized_migration_filename_fails_before_database_changes(
    tmp_path: Path,
) -> None:
    (tmp_path / "create_example.sql").write_text("SELECT 1;", encoding="utf-8")
    conn = MagicMock()
    conn.execute = AsyncMock()

    with (
        patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path),
        pytest.raises(ValueError, match="invalid Scholight migration filename"),
    ):
        await apply_migrations(conn)

    conn.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_migration_version_fails_before_database_changes(tmp_path: Path) -> None:
    (tmp_path / "001_create_example.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "001_create_other.sql").write_text("SELECT 2;", encoding="utf-8")
    conn = MagicMock()
    conn.execute = AsyncMock()

    with (
        patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path),
        pytest.raises(ValueError, match="duplicate Scholight migration version"),
    ):
        await apply_migrations(conn)

    conn.execute.assert_not_awaited()


def test_anonymous_quota_migration_has_exact_additive_schema() -> None:
    migration = Path(__file__).parents[3] / "migrations/006_create_anonymous_daily_search_usage.sql"

    sql = migration.read_text(encoding="utf-8")

    assert "CREATE TABLE public.anonymous_daily_search_usage" in sql
    assert "PRIMARY KEY (quota_date, ip_digest, search_level)" in sql
    assert "CHECK (octet_length(ip_digest) = 32)" in sql
    assert "CHECK (search_level IN (1, 2))" in sql
    assert "CHECK (used_count >= 0)" in sql
    assert "IF NOT EXISTS" not in sql
    assert "CREATE INDEX" not in sql
