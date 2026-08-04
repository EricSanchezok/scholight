"""PostgreSQL queue query contracts."""

from __future__ import annotations

import datetime as dt
import os
from unittest.mock import AsyncMock, patch

import pytest

from scholight.db.client import DBError
from scholight.db.queries_ingestion import (
    claim_ingestion_job,
    enqueue_ingestion_job,
    mark_sync_succeeded,
    release_ingestion_job,
    renew_ingestion_job_lease,
)
from scholight.db.tests.pg_ingestion_support import isolated_database_url


@pytest.mark.asyncio
async def test_enqueue_uses_single_upsert_for_version_promotion() -> None:
    pool = AsyncMock()
    pool.fetchrow.return_value = {"arxiv_id": "2401.00001"}

    with patch("scholight.db.queries_ingestion.get_pool", return_value=pool):
        changed = await enqueue_ingestion_job(
            "2401.00001",
            2,
            "revision",
            max_attempts=8,
        )

    assert changed is True
    assert "GREATEST" in pool.fetchrow.call_args.args[0]
    assert "ON CONFLICT (arxiv_id)" in pool.fetchrow.call_args.args[0]


@pytest.mark.asyncio
async def test_claim_uses_skip_locked_and_expiring_lease() -> None:
    pool = AsyncMock()
    pool.fetchrow.return_value = None

    with patch("scholight.db.queries_ingestion.get_pool", return_value=pool):
        result = await claim_ingestion_job("worker-1", 7200)

    query = pool.fetchrow.call_args.args[0]
    assert result is None
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "lease_expires_at <= now()" in query
    assert "CASE WHEN status = 'retry' THEN 0 ELSE 1 END" in query


@pytest.mark.asyncio
async def test_lease_renewal_requires_the_current_running_owner() -> None:
    pool = AsyncMock()
    pool.execute.return_value = "UPDATE 1"

    with patch("scholight.db.queries_ingestion.get_pool", return_value=pool):
        renewed = await renew_ingestion_job_lease("2401.00001", "worker-1", 7200)

    query = pool.execute.call_args.args[0]
    assert renewed is True
    assert "status = 'running'" in query
    assert "lease_owner = $2" in query
    assert "make_interval(secs => $3)" in query


@pytest.mark.asyncio
async def test_release_returns_interrupted_work_without_spending_an_attempt() -> None:
    pool = AsyncMock()
    pool.execute.return_value = "UPDATE 1"

    with patch("scholight.db.queries_ingestion.get_pool", return_value=pool):
        released = await release_ingestion_job("2401.00001", "worker-1")

    query = pool.execute.call_args.args[0]
    assert released is True
    assert "status = 'pending'" in query
    assert "GREATEST(attempt_count - 1, 0)" in query
    assert "lease_owner = $2" in query


@pytest.mark.asyncio
async def test_cursor_refuses_to_skip_a_date() -> None:
    pool = AsyncMock()
    pool.execute.return_value = "UPDATE 0"

    with patch("scholight.db.queries_ingestion.get_pool", return_value=pool):
        with pytest.raises(DBError, match="skip"):
            await mark_sync_succeeded("arxiv", dt.date(2026, 7, 23))


def test_destructive_pg_tests_reject_a_remote_database() -> None:
    with (
        patch.dict(
            os.environ,
            {
                "SCHOLIGHT_TEST_DATABASE_URL": (
                    "postgresql://postgres:secret@production.example/scholight_test_ingestion"
                )
            },
        ),
        pytest.raises(RuntimeError, match="loopback"),
    ):
        isolated_database_url()
