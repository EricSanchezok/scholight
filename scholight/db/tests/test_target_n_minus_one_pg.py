"""The retained N-1 SQL facade remains usable after destination schema expansion."""

import datetime as dt
import hashlib
from pathlib import Path
from unittest.mock import patch

import asyncpg
import pytest

from scholight.db import queries_ingestion as legacy
from scholight.db.tests.pg_ingestion_support import isolated_database_url, reset_ingestion_database

pytestmark = pytest.mark.pg_integration


@pytest.mark.asyncio
async def test_reviewed_n_minus_one_queue_and_cursor_still_work_after_016() -> None:
    assert hashlib.sha256(Path(legacy.__file__).read_bytes()).hexdigest() == (
        "e5f61a6aec687c733493b8e56239d03235f15aceb43e147e6d16e04dfbe1b1ce"
    )
    pool = await asyncpg.create_pool(isolated_database_url(), min_size=1, max_size=2)
    try:
        await reset_ingestion_database(pool)
        with patch.object(legacy, "get_pool", return_value=pool):
            assert await legacy.enqueue_ingestion_job("2609.00001", 1, "new", max_attempts=8)
            claimed = await legacy.claim_ingestion_job("n-minus-one", 300)
            assert claimed is not None
            await legacy.complete_ingestion_job(claimed.arxiv_id, "n-minus-one")
            await legacy.mark_sync_started("arxiv")
            await legacy.mark_sync_succeeded("arxiv", dt.date(2026, 9, 12))
        assert await pool.fetchval("SELECT status FROM scholight.ingestion_jobs") == "succeeded"
        assert await pool.fetchval(
            "SELECT last_successful_date FROM scholight.ingestion_sync_state"
        ) == dt.date(2026, 9, 12)
        assert await pool.fetchval("SELECT count(*) FROM scholight.target_ingestion_jobs") == 0
    finally:
        await pool.close()
