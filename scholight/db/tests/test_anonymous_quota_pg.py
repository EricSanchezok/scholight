"""Isolated PostgreSQL gates for anonymous quota concurrency."""

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
from scholight.db.queries_anonymous_quota import reserve_anonymous_daily_quota

pytestmark = pytest.mark.pg_integration
_BASELINE = Path(__file__).parents[3] / "migrations/001_scholight_baseline.sql"


def _database_url() -> str:
    try:
        return os.environ["SCHOLIGHT_TEST_DATABASE_URL"]
    except KeyError as exc:
        raise RuntimeError("SCHOLIGHT_TEST_DATABASE_URL must target an isolated database") from exc


async def _reset_database(pool: asyncpg.Pool) -> None:
    await pool.execute("DROP SCHEMA IF EXISTS scholight CASCADE")
    await pool.execute("DROP SCHEMA IF EXISTS auth CASCADE")
    await pool.execute("CREATE SCHEMA auth")
    await pool.execute("CREATE SCHEMA scholight")
    await pool.execute("CREATE TABLE auth.users (id BIGINT PRIMARY KEY)")
    await pool.execute(_BASELINE.read_text(encoding="utf-8"))


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


async def _run_concurrent_reservations(
    *, strength: str, limit: int, attempts: int
) -> tuple[int, int]:
    pool = await asyncpg.create_pool(_database_url(), min_size=1, max_size=20)
    try:
        await _reset_database(pool)
        with patch("scholight.db.queries_anonymous_quota.get_pool", return_value=pool):
            reservations = await asyncio.gather(
                *(
                    reserve_anonymous_daily_quota(
                        b"d" * 32,
                        strength=strength,
                        limit=limit,
                    )
                    for _ in range(attempts)
                )
            )
        successful = sum(reservation is not None for reservation in reservations)
        used_count = await pool.fetchval(
            "SELECT used_count FROM scholight.anonymous_daily_search_usage "
            "WHERE ip_digest = $1 AND strength = $2",
            b"d" * 32,
            strength,
        )
        return successful, used_count
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_standard_quota_allows_exactly_100_of_200_concurrent_requests() -> None:
    result = await _run_concurrent_reservations(strength="standard", limit=100, attempts=200)

    assert result == (100, 100)


@pytest.mark.asyncio
async def test_thorough_quota_allows_exactly_30_of_100_concurrent_requests() -> None:
    result = await _run_concurrent_reservations(strength="thorough", limit=30, attempts=100)

    assert result == (30, 30)
