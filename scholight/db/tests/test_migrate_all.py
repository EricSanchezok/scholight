"""Unified cloud-auth and Scholight migration orchestration tests."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from pathlib import Path
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from scholight.db.migrate import _MIGRATION_LOCK_ID
from scholight.db.migrate_all import run_all_migrations


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
async def test_run_all_migrations_orders_auth_before_scholight(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "001_create_auth.sql").write_text("SELECT 1;", encoding="utf-8")
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(conn)
    events: list[str] = []

    async def apply_auth(connection: object, migrations_dir: Path) -> None:
        assert connection is conn
        assert migrations_dir == auth_dir
        events.append("auth")

    async def apply_scholight(connection: object) -> None:
        assert connection is conn
        events.append("scholight")

    with (
        patch("scholight.db.migrate_all.apply_auth_migrations", side_effect=apply_auth),
        patch("scholight.db.migrate_all.apply_migrations", side_effect=apply_scholight),
    ):
        await run_all_migrations(pool, auth_migrations_dir=auth_dir)

    assert events == ["auth", "scholight"]
    assert conn.execute.await_args_list == [
        call("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_ID),
        call("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_ID),
    ]


@pytest.mark.asyncio
async def test_run_all_migrations_stops_after_auth_failure(tmp_path: Path) -> None:
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir()
    (auth_dir / "001_create_auth.sql").write_text("SELECT 1;", encoding="utf-8")
    conn = MagicMock()
    conn.execute = AsyncMock()
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(conn)

    with (
        patch(
            "scholight.db.migrate_all.apply_auth_migrations",
            AsyncMock(side_effect=RuntimeError("auth migration failed")),
        ),
        patch("scholight.db.migrate_all.apply_migrations", AsyncMock()) as apply_scholight,
        pytest.raises(RuntimeError, match="auth migration failed"),
    ):
        await run_all_migrations(pool, auth_migrations_dir=auth_dir)

    apply_scholight.assert_not_awaited()
    assert conn.execute.await_args_list[-1] == call(
        "SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_ID
    )


@pytest.mark.asyncio
async def test_run_all_migrations_rejects_missing_auth_migrations(tmp_path: Path) -> None:
    pool = MagicMock()

    with pytest.raises(FileNotFoundError, match="cloud-auth migration files"):
        await run_all_migrations(pool, auth_migrations_dir=tmp_path / "missing")

    pool.acquire.assert_not_called()


def test_expand_only_policy_rejects_destructive_sql() -> None:
    from scholight.db.migration_policy import validate_expand_only_sql

    with pytest.raises(ValueError, match="destructive migration"):
        validate_expand_only_sql("ALTER TABLE users DROP COLUMN legacy_token;")


def test_expand_only_policy_allows_destructive_sql_only_as_explicit_contract() -> None:
    from scholight.db.migration_policy import validate_expand_only_sql

    validate_expand_only_sql(
        "-- scholight: migration-phase=contract\nALTER TABLE users DROP COLUMN legacy_token;",
        allow_contract=True,
    )


def test_expand_only_policy_ignores_keywords_in_comments() -> None:
    from scholight.db.migration_policy import validate_expand_only_sql

    validate_expand_only_sql("-- do not DROP TABLE users\nCREATE TABLE example (id BIGINT);")
