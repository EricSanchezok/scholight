"""Isolated PostgreSQL gates for migration 006 and anonymous quota concurrency."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

import asyncpg
import pytest

from scholight.config import settings
from scholight.db import client as db_client
from scholight.db.migrate import run_migrations
from scholight.db.queries_anonymous_quota import reserve_anonymous_daily_quota

pytestmark = pytest.mark.pg_integration
_MIGRATION = Path(__file__).parents[3] / "migrations/006_create_anonymous_daily_search_usage.sql"


def _database_url() -> str:
    try:
        return os.environ["SCHOLIGHT_TEST_DATABASE_URL"]
    except KeyError as exc:
        raise RuntimeError("SCHOLIGHT_TEST_DATABASE_URL must target an isolated database") from exc


async def _reset_database() -> None:
    connection = await asyncpg.connect(_database_url())
    try:
        await connection.execute("DROP TABLE IF EXISTS public.anonymous_daily_search_usage")
        await connection.execute("DROP TABLE IF EXISTS public._migrations")
    finally:
        await connection.close()


async def _create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(_database_url(), min_size=1, max_size=20)


def _copy_migration(directory: Path, *, broken: bool = False) -> None:
    sql = _MIGRATION.read_text(encoding="utf-8")
    if broken:
        sql += "\nSELECT * FROM public.missing_migration_dependency;\n"
    (directory / _MIGRATION.name).write_text(sql, encoding="utf-8")


@pytest.mark.asyncio
async def test_application_pool_sessions_use_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = urlsplit(_database_url())
    monkeypatch.setattr(db_client, "_pool", None)
    monkeypatch.setattr(settings, "pg_host", parsed.hostname or "127.0.0.1")
    monkeypatch.setattr(settings, "pg_port", parsed.port or 5432)
    monkeypatch.setattr(settings, "pg_database", parsed.path.lstrip("/") or "postgres")
    monkeypatch.setattr(settings, "pg_user", unquote(parsed.username or "postgres"))
    monkeypatch.setattr(settings, "pg_password", unquote(parsed.password or ""))
    monkeypatch.setattr(settings, "pg_ssl_root_cert", "disable")
    monkeypatch.setattr(settings, "pg_pool_min_size", 1)
    monkeypatch.setattr(settings, "pg_pool_max_size", 1)

    pool = await db_client.create_pool()
    try:
        timezone = await pool.fetchval("SHOW TimeZone")
    finally:
        await db_client.close_pool()

    assert timezone == "UTC"


@pytest.mark.asyncio
async def test_migration_006_applies_to_fresh_database(tmp_path: Path) -> None:
    await _reset_database()
    _copy_migration(tmp_path)
    pool = await _create_pool()
    try:
        with patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path):
            await run_migrations(pool)
        state = await pool.fetchrow(
            "SELECT to_regclass('public.anonymous_daily_search_usage') AS table_name, "
            "(SELECT name FROM public._migrations WHERE version = 6) AS migration_name"
        )
    finally:
        await pool.close()
        await _reset_database()

    assert tuple(state.values()) == (
        "anonymous_daily_search_usage",
        "create_anonymous_daily_search_usage",
    )


@pytest.mark.asyncio
async def test_migration_006_upgrades_version_005_database(tmp_path: Path) -> None:
    await _reset_database()
    _copy_migration(tmp_path)
    pool = await _create_pool()
    try:
        await pool.execute(
            "CREATE TABLE public._migrations ("
            "version INTEGER PRIMARY KEY, name TEXT NOT NULL, "
            "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        await pool.execute(
            "INSERT INTO public._migrations (version, name) VALUES (5, 'create_search_history')"
        )
        with patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path):
            await run_migrations(pool)
        versions = await pool.fetch("SELECT version FROM public._migrations ORDER BY version")
    finally:
        await pool.close()
        await _reset_database()

    assert [row["version"] for row in versions] == [5, 6]


@pytest.mark.asyncio
async def test_migration_006_is_idempotent_through_runner(tmp_path: Path) -> None:
    await _reset_database()
    _copy_migration(tmp_path)
    pool = await _create_pool()
    try:
        with patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path):
            await run_migrations(pool)
            await run_migrations(pool)
        migration_count = await pool.fetchval(
            "SELECT count(*) FROM public._migrations WHERE version = 6"
        )
    finally:
        await pool.close()
        await _reset_database()

    assert migration_count == 1


@pytest.mark.asyncio
async def test_migration_failure_rolls_back_ddl_and_tracking(tmp_path: Path) -> None:
    await _reset_database()
    _copy_migration(tmp_path, broken=True)
    pool = await _create_pool()
    try:
        with (
            patch("scholight.db.migrate._MIGRATIONS_DIR", tmp_path),
            pytest.raises(asyncpg.UndefinedTableError),
        ):
            await run_migrations(pool)
        state = await pool.fetchrow(
            "SELECT to_regclass('public.anonymous_daily_search_usage') AS table_name, "
            "EXISTS(SELECT 1 FROM public._migrations WHERE version = 6) AS tracked"
        )
    finally:
        await pool.close()
        await _reset_database()

    assert tuple(state.values()) == (None, False)


async def _run_concurrent_reservations(
    *, search_level: int, limit: int, attempts: int
) -> tuple[int, int]:
    await _reset_database()
    pool = await _create_pool()
    try:
        await pool.execute(_MIGRATION.read_text(encoding="utf-8"))
        with patch("scholight.db.queries_anonymous_quota.get_pool", return_value=pool):
            reservations = await asyncio.gather(
                *(
                    reserve_anonymous_daily_quota(
                        b"d" * 32,
                        search_level=search_level,
                        limit=limit,
                    )
                    for _ in range(attempts)
                )
            )
        successful = sum(reservation is not None for reservation in reservations)
        used_count = await pool.fetchval(
            "SELECT used_count FROM public.anonymous_daily_search_usage "
            "WHERE ip_digest = $1 AND search_level = $2",
            b"d" * 32,
            search_level,
        )
        return successful, used_count
    finally:
        await pool.close()
        await _reset_database()


@pytest.mark.asyncio
async def test_standard_quota_allows_exactly_100_of_200_concurrent_requests() -> None:
    result = await _run_concurrent_reservations(search_level=1, limit=100, attempts=200)

    assert result == (100, 100)


@pytest.mark.asyncio
async def test_thorough_quota_allows_exactly_30_of_100_concurrent_requests() -> None:
    result = await _run_concurrent_reservations(search_level=2, limit=30, attempts=100)

    assert result == (30, 30)
