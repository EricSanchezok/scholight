"""Baseline adoption retains the selected recovery scope and never consults old success."""

import datetime as dt
from collections.abc import AsyncIterator
from unittest.mock import patch

import asyncpg
import pytest
import pytest_asyncio

from scholight.db.target_adoption import seed_scope
from scholight.db.target_ingestion import TargetQueue, register_target
from scholight.db.tests.pg_ingestion_support import isolated_database_url, reset_ingestion_database
from scholight.db.tests.test_target_ingestion_pg import target

pytestmark = pytest.mark.pg_integration


@pytest_asyncio.fixture
async def pool() -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(isolated_database_url(), min_size=1, max_size=2)
    await reset_ingestion_database(pool)
    try:
        with (
            patch("scholight.db.target_adoption.get_pool", return_value=pool),
            patch("scholight.db.target_ingestion.get_pool", return_value=pool),
        ):
            yield pool
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_scope_unions_deferred_and_related_failures_without_global_backfill(
    pool: asyncpg.Pool,
) -> None:
    t = target()
    await register_target(t)
    await pool.execute("""INSERT INTO scholight.deferred_fulltext(arxiv_id,target_version,first_seen_date,last_seen_date)
    VALUES ('2608.00001',2,'2026-08-04','2026-09-12'),('2001.00001',1,'2020-01-01','2020-01-01')""")
    await pool.execute("""INSERT INTO scholight.ingestion_jobs(arxiv_id,target_version,source,priority,status,updated_at,max_attempts)
    VALUES('2609.00001',1,'new',10,'dead','2026-09-11',8),('2101.00001',1,'new',10,'dead','2021-01-01',8)""")
    result = await seed_scope(TargetQueue(t.key), dt.date(2026, 8, 4), dt.date(2026, 9, 12))
    assert result == 2
    assert await pool.fetchval("SELECT count(*) FROM scholight.fulltext_scope") == 2
    assert await pool.fetchval("SELECT count(*) FROM scholight.ingestion_jobs") == 2
    assert await seed_scope(TargetQueue(t.key), dt.date(2026, 8, 4), dt.date(2026, 9, 12)) == 2
    assert await pool.fetchval("SELECT count(*) FROM scholight.fulltext_scope") == 2
