"""Real PostgreSQL concurrency contracts for Scholight Survey jobs."""

from __future__ import annotations

import asyncio
import datetime as dt
from unittest.mock import patch
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from scholight.db.queries_survey import (
    SurveyQuotaExceededError,
    claim_survey_job,
    create_survey_job,
    delete_pending_survey_job,
    get_survey_usage,
    recover_expired_survey_jobs,
    settle_survey_execution,
)
from scholight.db.tests.pg_ingestion_support import (
    isolated_database_url,
    reset_ingestion_database,
)

pytestmark = pytest.mark.pg_integration


@pytest_asyncio.fixture
async def survey_pool() -> asyncpg.Pool:
    pool = await asyncpg.create_pool(isolated_database_url(), min_size=1, max_size=12)
    await reset_ingestion_database(pool)
    await pool.execute("INSERT INTO auth.users (id) VALUES (42)")
    try:
        yield pool
    finally:
        await pool.close()


@pytest.mark.asyncio
async def test_six_concurrent_submissions_reserve_exactly_five(
    survey_pool: asyncpg.Pool,
) -> None:
    async def _submit() -> str:
        try:
            await create_survey_job(
                job_id=uuid4(),
                user_id=42,
                topic="retrieval augmented generation",
                quota_date=dt.date(2026, 7, 31),
                daily_limit=5,
            )
        except SurveyQuotaExceededError:
            return "rejected"
        return "created"

    with patch("scholight.db.queries_survey.get_pool", return_value=survey_pool):
        results = await asyncio.gather(*(_submit() for _ in range(6)))
        usage = await get_survey_usage(
            user_id=42,
            usage_date=dt.date(2026, 7, 31),
        )

    assert results.count("created") == 5
    assert results.count("rejected") == 1
    assert usage == (5, 0)


@pytest.mark.asyncio
async def test_two_workers_claim_distinct_jobs_and_settlement_is_idempotent(
    survey_pool: asyncpg.Pool,
) -> None:
    worker_a = uuid4()
    worker_b = uuid4()
    quota_date = dt.date(2026, 7, 31)
    with patch("scholight.db.queries_survey.get_pool", return_value=survey_pool):
        for topic in ("retrieval", "reasoning"):
            await create_survey_job(
                job_id=uuid4(),
                user_id=42,
                topic=topic,
                quota_date=quota_date,
                daily_limit=5,
            )
        first, second = await asyncio.gather(
            claim_survey_job(worker_id=worker_a, lease_seconds=3600),
            claim_survey_job(worker_id=worker_b, lease_seconds=3600),
        )
        assert first is not None and second is not None
        settled = await settle_survey_execution(
            job_id=first.id,
            worker_id=first.lease_owner or worker_a,
            outcome="succeeded",
            error_code=None,
            error_message=None,
        )
        repeated = await settle_survey_execution(
            job_id=first.id,
            worker_id=first.lease_owner or worker_a,
            outcome="succeeded",
            error_code=None,
            error_message=None,
        )
        usage = await get_survey_usage(user_id=42, usage_date=quota_date)

    assert first.id != second.id
    assert settled.status == "archiving"
    assert repeated.id == settled.id
    assert usage == (1, 1)


@pytest.mark.asyncio
async def test_failed_execution_and_pending_cancellation_release_reservations(
    survey_pool: asyncpg.Pool,
) -> None:
    quota_date = dt.date(2026, 7, 31)
    cancelled_id = uuid4()
    with patch("scholight.db.queries_survey.get_pool", return_value=survey_pool):
        await create_survey_job(
            job_id=cancelled_id,
            user_id=42,
            topic="cancelled",
            quota_date=quota_date,
            daily_limit=5,
        )
        await create_survey_job(
            job_id=uuid4(),
            user_id=42,
            topic="failed",
            quota_date=quota_date,
            daily_limit=5,
        )
        assert await delete_pending_survey_job(job_id=cancelled_id, user_id=42)
        claimed = await claim_survey_job(worker_id=uuid4(), lease_seconds=3600)
        assert claimed is not None
        await settle_survey_execution(
            job_id=claimed.id,
            worker_id=claimed.lease_owner or uuid4(),
            outcome="failed",
            error_code="survey_execution_failed",
            error_message="Survey generation did not complete successfully.",
        )
        usage = await get_survey_usage(user_id=42, usage_date=quota_date)

    assert usage == (0, 0)


@pytest.mark.asyncio
async def test_expired_running_job_survives_pool_restart_and_recovers_for_archiving() -> None:
    database_url = isolated_database_url()
    quota_date = dt.date(2026, 7, 31)
    first_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=4)
    await reset_ingestion_database(first_pool)
    await first_pool.execute("INSERT INTO auth.users (id) VALUES (42)")
    with patch("scholight.db.queries_survey.get_pool", return_value=first_pool):
        await create_survey_job(
            job_id=uuid4(),
            user_id=42,
            topic="recover after restart",
            quota_date=quota_date,
            daily_limit=5,
        )
        claimed = await claim_survey_job(worker_id=uuid4(), lease_seconds=3600)
    assert claimed is not None
    await first_pool.execute(
        "UPDATE scholight.survey_jobs SET lease_expires_at = now() - interval '1 second' "
        "WHERE id = $1",
        claimed.id,
    )
    await first_pool.close()

    restarted_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=4)
    try:
        with patch("scholight.db.queries_survey.get_pool", return_value=restarted_pool):
            assert await recover_expired_survey_jobs() == 1
            archive_job = await claim_survey_job(worker_id=uuid4(), lease_seconds=3600)
            usage = await get_survey_usage(user_id=42, usage_date=quota_date)
        assert archive_job is not None
        assert archive_job.id == claimed.id
        assert archive_job.status == "archiving"
        assert archive_job.terminal_outcome == "failed"
        assert archive_job.error_code == "survey_worker_lost"
        assert usage == (0, 0)
    finally:
        await restarted_pool.close()
