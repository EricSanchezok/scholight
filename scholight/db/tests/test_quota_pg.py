"""PostgreSQL concurrency contract for Scholight-owned user quotas."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import patch

import asyncpg
import pytest

from scholight.db.queries_quota import reserve_user_quota

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
    await pool.execute("INSERT INTO auth.users (id) VALUES (42)")
    await pool.execute(_BASELINE.read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_override_allows_exactly_17_of_100_concurrent_reservations() -> None:
    pool = await asyncpg.create_pool(_database_url(), min_size=1, max_size=20)
    try:
        await _reset_database(pool)
        await pool.execute(
            "INSERT INTO scholight.user_quota_overrides "
            "(user_id, strength, daily_limit) VALUES (42, 'thorough', 17)"
        )
        with patch("scholight.db.queries_quota.get_pool", return_value=pool):
            reservations = await asyncio.gather(
                *(
                    reserve_user_quota(
                        42,
                        strength="thorough",
                        default_limit=1000,
                    )
                    for _ in range(100)
                )
            )

        successful = [reservation for reservation in reservations if reservation is not None]
        used = await pool.fetchval(
            "SELECT used_count FROM scholight.user_daily_search_usage "
            "WHERE user_id = 42 AND strength = 'thorough'"
        )

        assert (len(successful), used) == (17, 17)
        assert {reservation.daily_limit for reservation in successful} == {17}
    finally:
        await pool.close()
