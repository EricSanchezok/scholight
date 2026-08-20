"""Real PostgreSQL concurrency contracts for Survey aggregates and Drafts."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from scholight.db.queries_survey import (
    SurveyQuotaExceededError,
    SurveyStateError,
    cancel_survey,
    claim_survey_job,
    create_survey,
    delete_survey,
    finish_survey_archive,
    get_survey,
    get_survey_progress,
    heartbeat_survey_job,
    mark_survey_workspace_missing,
    recover_archived_survey_contract_failure,
    recover_expired_survey_jobs,
    settle_survey_execution,
    start_survey,
    update_survey_job_progress,
)
from scholight.db.queries_survey_capacity import get_survey_capacity_snapshot
from scholight.db.queries_survey_cleanup import get_artifact_cleanup_status
from scholight.db.queries_survey_drafts import (
    SurveyDraftLimitError,
    claim_survey_draft,
    complete_survey_draft,
    create_manual_draft,
    fail_survey_draft,
    list_survey_drafts,
    request_generated_draft,
)
from scholight.db.queries_survey_notifications import (
    claim_email_notification,
    complete_email_notification,
)
from scholight.db.queries_survey_views import (
    get_survey_artifact_reference,
    list_survey_summaries,
)
from scholight.db.tests.pg_ingestion_support import (
    isolated_database_url,
    reset_ingestion_database,
)
from scholight.survey.contracts import SurveyLeaseLostError

pytestmark = pytest.mark.pg_integration
_QUOTA_DATE = dt.date(2026, 7, 31)
_MIGRATIONS = Path(__file__).parents[3] / "migrations"


@pytest_asyncio.fixture
async def survey_pool() -> asyncpg.Pool:
    async def _set_short_lock_timeout(connection: asyncpg.Connection) -> None:
        await connection.execute("SET lock_timeout = '2s'")

    pool = await asyncpg.create_pool(
        isolated_database_url(),
        min_size=1,
        max_size=12,
        init=_set_short_lock_timeout,
    )
    await reset_ingestion_database(pool)
    await pool.execute(
        "INSERT INTO auth.users (id, email, email_verified_at) VALUES "
        "(42, 'reader@example.com', now()), (43, 'second@example.com', now())"
    )
    try:
        yield pool
    finally:
        await pool.close()


async def _create(
    *,
    request_id: UUID | None = None,
    user_id: int = 42,
    request_hash: str = "0" * 64,
    daily_limit: int = 5,
) -> UUID:
    survey_id = uuid4()
    survey = await create_survey(
        survey_id=survey_id,
        draft_id=uuid4(),
        user_id=user_id,
        initial_request="retrieval augmented generation",
        client_request_id=request_id or uuid4(),
        request_hash=request_hash,
        quota_date=_QUOTA_DATE,
        daily_limit=daily_limit,
    )
    return survey.id


async def _complete_next_draft(*, markdown: str) -> None:
    worker_id = uuid4()
    draft = await claim_survey_draft(worker_id=worker_id, lease_seconds=3600)
    assert draft is not None
    await complete_survey_draft(draft_id=draft.id, worker_id=worker_id, markdown=markdown)


async def _usage(pool: asyncpg.Pool) -> tuple[int, int]:
    row = await pool.fetchrow(
        "SELECT reserved_count, succeeded_count FROM scholight.survey_daily_usage "
        "WHERE user_id = 42 AND usage_date = $1",
        _QUOTA_DATE,
    )
    assert row is not None
    return int(row["reserved_count"]), int(row["succeeded_count"])


@pytest.mark.asyncio
async def test_six_concurrent_creates_reserve_exactly_five(survey_pool: asyncpg.Pool) -> None:
    async def _submit() -> str:
        try:
            await _create()
        except SurveyQuotaExceededError:
            return "rejected"
        return "created"

    with patch("scholight.db.queries_survey.get_pool", return_value=survey_pool):
        results = await asyncio.gather(*(_submit() for _ in range(6)))

    assert results.count("created") == 5
    assert results.count("rejected") == 1
    assert await _usage(survey_pool) == (5, 0)


@pytest.mark.asyncio
async def test_create_survey_enforces_user_override(survey_pool: asyncpg.Pool) -> None:
    await survey_pool.execute(
        "INSERT INTO scholight.user_quota_overrides "
        "(user_id, strength, daily_limit) VALUES (42, 'survey', 2)"
    )
    with patch("scholight.db.queries_survey.get_pool", return_value=survey_pool):
        await _create()
        await _create()

        with pytest.raises(SurveyQuotaExceededError):
            await _create()


@pytest.mark.asyncio
async def test_upgraded_daily_quota_does_not_raise_full_survey_user_concurrency(
    survey_pool: asyncpg.Pool,
) -> None:
    await survey_pool.execute(
        "INSERT INTO scholight.user_quota_overrides "
        "(user_id, strength, daily_limit) VALUES (42, 'survey', 10)"
    )
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_ids = [await _create(daily_limit=3) for _ in range(10)]
        for revision in range(10):
            await _complete_next_draft(markdown=f"# Approved Draft {revision}")
        for survey_id in survey_ids:
            await start_survey(
                survey_id=survey_id,
                user_id=42,
                job_id=uuid4(),
                client_request_id=uuid4(),
                request_hash="a" * 64,
            )
        claimed = await asyncio.gather(
            *(
                claim_survey_job(
                    worker_id=uuid4(),
                    lease_seconds=3600,
                    global_concurrency=16,
                    per_user_concurrency=4,
                )
                for _ in range(10)
            )
        )

    assert sum(job is not None for job in claimed) == 4


@pytest.mark.asyncio
async def test_create_idempotency_does_not_double_reserve(survey_pool: asyncpg.Pool) -> None:
    request_id = uuid4()
    with patch("scholight.db.queries_survey.get_pool", return_value=survey_pool):
        first_id = await _create(request_id=request_id)
        second_id = await _create(request_id=request_id)

    assert second_id == first_id
    assert await _usage(survey_pool) == (1, 0)
    assert await survey_pool.fetchval("SELECT count(*) FROM scholight.survey_drafts") == 1


@pytest.mark.asyncio
async def test_create_idempotency_rejects_different_payload(survey_pool: asyncpg.Pool) -> None:
    request_id = uuid4()
    with patch("scholight.db.queries_survey.get_pool", return_value=survey_pool):
        await _create(request_id=request_id, request_hash="a" * 64)
        with pytest.raises(SurveyStateError) as error:
            await _create(request_id=request_id, request_hash="b" * 64)

    assert error.value.code == "survey_idempotency_conflict"


@pytest.mark.asyncio
async def test_generated_draft_idempotency_rejects_different_payload(
    survey_pool: asyncpg.Pool,
) -> None:
    request_id = uuid4()
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Draft 1")
        await request_generated_draft(
            survey_id=survey_id,
            user_id=42,
            draft_id=uuid4(),
            client_request_id=request_id,
            request_hash="a" * 64,
            user_message="Focus on evaluation.",
        )
        with pytest.raises(SurveyStateError) as error:
            await request_generated_draft(
                survey_id=survey_id,
                user_id=42,
                draft_id=uuid4(),
                client_request_id=request_id,
                request_hash="b" * 64,
                user_message="Focus on deployment.",
            )

    assert error.value.code == "survey_idempotency_conflict"
    assert (
        await survey_pool.fetchval(
            "SELECT count(*) FROM scholight.survey_drafts WHERE survey_id = $1", survey_id
        )
        == 2
    )


@pytest.mark.asyncio
async def test_manual_draft_idempotency_rejects_different_payload(
    survey_pool: asyncpg.Pool,
) -> None:
    request_id = uuid4()
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Draft 1")
        first = await create_manual_draft(
            survey_id=survey_id,
            user_id=42,
            draft_id=uuid4(),
            client_request_id=request_id,
            request_hash="a" * 64,
            user_message="Use this scope.",
            markdown="# Manual scope",
        )
        repeated = await create_manual_draft(
            survey_id=survey_id,
            user_id=42,
            draft_id=uuid4(),
            client_request_id=request_id,
            request_hash="a" * 64,
            user_message="Use this scope.",
            markdown="# Manual scope",
        )
        with pytest.raises(SurveyStateError) as error:
            await create_manual_draft(
                survey_id=survey_id,
                user_id=42,
                draft_id=uuid4(),
                client_request_id=request_id,
                request_hash="b" * 64,
                user_message="Use a different scope.",
                markdown="# Different scope",
            )

    assert repeated.id == first.id
    assert error.value.code == "survey_idempotency_conflict"


@pytest.mark.asyncio
async def test_failed_draft_has_no_revision_and_manual_history_is_immutable(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        worker_id = uuid4()
        initial = await claim_survey_draft(worker_id=worker_id, lease_seconds=3600)
        assert initial is not None
        failed = await fail_survey_draft(
            draft_id=initial.id,
            worker_id=worker_id,
            error_code="survey_draft_generation_failed",
            error_message="Draft generation failed.",
        )
        manual = await create_manual_draft(
            survey_id=survey_id,
            user_id=42,
            draft_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="1" * 64,
            user_message="Use this scope",
            markdown="# Scope v1",
        )
        rows = await list_survey_drafts(survey_id=survey_id, user_id=42)

    assert failed.revision is None
    assert manual.revision == 1
    assert [(row.status, row.revision) for row in rows] == [("failed", None), ("ready", 1)]


@pytest.mark.asyncio
async def test_ten_ready_revisions_are_allowed_and_eleventh_is_rejected(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Draft 1")
        for revision in range(2, 11):
            await create_manual_draft(
                survey_id=survey_id,
                user_id=42,
                draft_id=uuid4(),
                client_request_id=uuid4(),
                request_hash=f"{revision:064x}",
                user_message=f"Revision {revision}",
                markdown=f"# Draft {revision}",
            )
        with pytest.raises(SurveyDraftLimitError):
            await request_generated_draft(
                survey_id=survey_id,
                user_id=42,
                draft_id=uuid4(),
                client_request_id=uuid4(),
                request_hash="b" * 64,
                user_message="One more revision",
            )

    assert (
        await survey_pool.fetchval(
            "SELECT count(*) FROM scholight.survey_drafts "
            "WHERE survey_id = $1 AND status = 'ready'",
            survey_id,
        )
        == 10
    )


@pytest.mark.asyncio
async def test_concurrent_start_creates_one_job_bound_to_latest_draft(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Draft 1")
        latest = await create_manual_draft(
            survey_id=survey_id,
            user_id=42,
            draft_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="c" * 64,
            user_message="Final scope",
            markdown="# Draft 2",
        )

        async def _start() -> str:
            try:
                await start_survey(
                    survey_id=survey_id,
                    user_id=42,
                    job_id=uuid4(),
                    client_request_id=uuid4(),
                    request_hash="d" * 64,
                )
            except SurveyStateError:
                return "rejected"
            return "started"

        results = await asyncio.gather(_start(), _start())

    assert results.count("started") == 1
    assert results.count("rejected") == 1
    assert await survey_pool.fetchval("SELECT count(*) FROM scholight.survey_jobs") == 1
    assert (
        await survey_pool.fetchval(
            "SELECT approved_draft_id FROM scholight.survey_jobs WHERE survey_id = $1", survey_id
        )
        == latest.id
    )


@pytest.mark.asyncio
async def test_formal_failure_keeps_one_failed_survey_and_releases_quota(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="d" * 64,
            notify_on_completion=True,
        )
        worker_id = uuid4()
        job = await claim_survey_job(worker_id=worker_id, lease_seconds=3600)
        assert job is not None
        await settle_survey_execution(
            job_id=job.id,
            worker_id=worker_id,
            outcome="failed",
            error_code="survey_execution_failed",
            error_message="Survey generation failed.",
        )
        await finish_survey_archive(
            job_id=job.id,
            worker_id=worker_id,
            storage_bucket="test-surveys",
            storage_prefix="surveys/v1/42/test",
            manifest_key="surveys/v1/42/test/manifest.json",
        )
        survey = await get_survey(survey_id=survey_id, user_id=42)

    assert survey is not None
    assert survey.status == "failed"
    assert survey.quota_state == "released"
    assert await survey_pool.fetchval("SELECT count(*) FROM scholight.surveys") == 1
    notification = await survey_pool.fetchrow(
        "SELECT * FROM scholight.survey_email_notifications WHERE survey_id = $1", survey_id
    )
    assert notification is not None
    assert notification["survey_outcome"] == "failed"
    assert notification["status"] == "pending"
    assert await _usage(survey_pool) == (0, 0)


@pytest.mark.asyncio
async def test_degraded_success_is_readable_and_does_not_consume_quota(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="f" * 64,
            notify_on_completion=True,
        )
        worker_id = uuid4()
        job = await claim_survey_job(worker_id=worker_id, lease_seconds=3600)
        assert job is not None
        settled = await settle_survey_execution(
            job_id=job.id,
            worker_id=worker_id,
            outcome="succeeded",
            error_code="survey_quality_degraded",
            error_message="Readable report delivered without charge.",
            chargeable=False,
        )
        await finish_survey_archive(
            job_id=job.id,
            worker_id=worker_id,
            storage_bucket="test-surveys",
            storage_prefix="surveys/v1/42/free-readable",
            manifest_key="surveys/v1/42/free-readable/manifest.json",
        )
        survey = await get_survey(survey_id=survey_id, user_id=42)

    assert settled.terminal_outcome == "succeeded"
    assert survey is not None
    assert survey.status == "succeeded"
    assert survey.quota_state == "released"
    assert survey.error_code == "survey_quality_degraded"
    assert await _usage(survey_pool) == (0, 0)
    notification = await survey_pool.fetchrow(
        "SELECT survey_outcome, status FROM scholight.survey_email_notifications "
        "WHERE survey_id = $1",
        survey_id,
    )
    assert notification is not None
    assert notification["survey_outcome"] == "succeeded"
    assert notification["status"] == "pending"


@pytest.mark.asyncio
async def test_verified_archived_contract_failure_recovers_atomically_and_idempotently(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        job_id = uuid4()
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=job_id,
            client_request_id=uuid4(),
            request_hash="a" * 64,
            notify_on_completion=True,
        )
        worker_id = uuid4()
        job = await claim_survey_job(worker_id=worker_id, lease_seconds=3600)
        assert job is not None
        await settle_survey_execution(
            job_id=job.id,
            worker_id=worker_id,
            outcome="failed",
            error_code="survey_contract_violation",
            error_message="An old identifier was misclassified.",
        )
        prefix = f"surveys/v1/42/{job.id}"
        manifest_key = f"{prefix}/manifest.json"
        await finish_survey_archive(
            job_id=job.id,
            worker_id=worker_id,
            storage_bucket="test-surveys",
            storage_prefix=prefix,
            manifest_key=manifest_key,
        )
        await survey_pool.execute(
            "UPDATE scholight.survey_email_notifications SET status = 'succeeded', "
            "attempts = 3, finished_at = now() WHERE survey_id = $1",
            survey_id,
        )

        assert await recover_archived_survey_contract_failure(
            job_id=job.id,
            expected_manifest_key=manifest_key,
            expected_error_code="survey_contract_violation",
            replacement_manifest_key=None,
        )
        assert not await recover_archived_survey_contract_failure(
            job_id=job.id,
            expected_manifest_key=manifest_key,
            expected_error_code="survey_contract_violation",
            replacement_manifest_key=None,
        )

    survey = await survey_pool.fetchrow(
        "SELECT status, quota_state, error_code, error_message FROM scholight.surveys "
        "WHERE id = $1",
        survey_id,
    )
    recovered_job = await survey_pool.fetchrow(
        "SELECT status, terminal_outcome, error_code, error_message "
        "FROM scholight.survey_jobs WHERE id = $1",
        job_id,
    )
    notification = await survey_pool.fetchrow(
        "SELECT status, survey_outcome, attempts FROM scholight.survey_email_notifications "
        "WHERE survey_id = $1",
        survey_id,
    )
    assert survey is not None and tuple(survey.values()) == (
        "succeeded",
        "consumed",
        None,
        None,
    )
    assert recovered_job is not None and tuple(recovered_job.values()) == (
        "finished",
        "succeeded",
        None,
        None,
    )
    assert notification is not None
    assert (notification["status"], notification["survey_outcome"]) == (
        "pending",
        "succeeded",
    )
    assert notification["attempts"] == 0
    assert await _usage(survey_pool) == (0, 1)


@pytest.mark.asyncio
async def test_archived_report_recovery_switches_manifest_once(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="b" * 64,
            notify_on_completion=True,
        )
        worker_id = uuid4()
        job = await claim_survey_job(worker_id=worker_id, lease_seconds=3600)
        assert job is not None
        await settle_survey_execution(
            job_id=job.id,
            worker_id=worker_id,
            outcome="failed",
            error_code="survey_report_missing",
            error_message="The report could not be assembled.",
        )
        prefix = f"surveys/v1/42/{job.id}"
        source_manifest_key = f"{prefix}/manifest.json"
        replacement_manifest_key = f"{prefix}/recoveries/{'a' * 64}/manifest.json"
        await finish_survey_archive(
            job_id=job.id,
            worker_id=worker_id,
            storage_bucket="test-surveys",
            storage_prefix=prefix,
            manifest_key=source_manifest_key,
        )

        assert await recover_archived_survey_contract_failure(
            job_id=job.id,
            expected_manifest_key=source_manifest_key,
            expected_error_code="survey_report_missing",
            replacement_manifest_key=replacement_manifest_key,
        )
        assert not await recover_archived_survey_contract_failure(
            job_id=job.id,
            expected_manifest_key=source_manifest_key,
            expected_error_code="survey_report_missing",
            replacement_manifest_key=replacement_manifest_key,
        )

    recovered_job = await survey_pool.fetchrow(
        "SELECT terminal_outcome, manifest_key FROM scholight.survey_jobs WHERE id = $1",
        job.id,
    )
    assert recovered_job is not None
    assert tuple(recovered_job.values()) == ("succeeded", replacement_manifest_key)
    assert await _usage(survey_pool) == (0, 1)


@pytest.mark.asyncio
async def test_unchecked_success_does_not_enqueue_email_notification(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="e" * 64,
        )
        worker_id = uuid4()
        job = await claim_survey_job(worker_id=worker_id, lease_seconds=3600)
        assert job is not None
        await settle_survey_execution(
            job_id=job.id,
            worker_id=worker_id,
            outcome="succeeded",
            error_code=None,
            error_message=None,
        )
        await finish_survey_archive(
            job_id=job.id,
            worker_id=worker_id,
            storage_bucket="test-surveys",
            storage_prefix="surveys/v1/42/test",
            manifest_key="surveys/v1/42/test/manifest.json",
        )

    assert (
        await survey_pool.fetchval(
            "SELECT count(*) FROM scholight.survey_email_notifications WHERE survey_id = $1",
            survey_id,
        )
        == 0
    )


@pytest.mark.asyncio
async def test_notification_claims_are_concurrent_and_use_current_account_email(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_notifications.get_pool", return_value=survey_pool),
    ):
        survey_ids = [await _create(user_id=42), await _create(user_id=43)]
        await survey_pool.execute(
            "UPDATE scholight.surveys SET status = 'failed', quota_state = 'released', "
            "finished_at = now(), title = 'Notification contract' WHERE id = ANY($1::uuid[])",
            survey_ids,
        )
        await survey_pool.executemany(
            "INSERT INTO scholight.survey_email_notifications "
            "(id, survey_id, user_id, survey_outcome) VALUES ($1, $2, $3, 'failed')",
            [(uuid4(), survey_ids[0], 42), (uuid4(), survey_ids[1], 43)],
        )
        await survey_pool.execute(
            "UPDATE auth.users SET email = 'current@example.com' WHERE id = 42"
        )
        first, second = await asyncio.gather(
            claim_email_notification(worker_id=uuid4(), lease_seconds=120),
            claim_email_notification(worker_id=uuid4(), lease_seconds=120),
        )

    assert first is not None and second is not None
    assert first.id != second.id
    assert {first.recipient_email, second.recipient_email} == {
        "current@example.com",
        "second@example.com",
    }
    assert first.lease_owner is not None
    with patch("scholight.db.queries_survey_notifications.get_pool", return_value=survey_pool):
        await complete_email_notification(notification_id=first.id, worker_id=first.lease_owner)
    assert (
        await survey_pool.fetchval(
            "SELECT count(*) FROM scholight.survey_email_notifications WHERE status = 'succeeded'"
        )
        == 1
    )


@pytest.mark.asyncio
async def test_account_cascade_preserves_artifact_cleanup_outbox(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="d" * 64,
        )
        worker_id = uuid4()
        job = await claim_survey_job(worker_id=worker_id, lease_seconds=3600)
        assert job is not None
        await settle_survey_execution(
            job_id=job.id,
            worker_id=worker_id,
            outcome="succeeded",
            error_code=None,
            error_message=None,
        )
        prefix = f"surveys/v1/42/{job.id}"
        await finish_survey_archive(
            job_id=job.id,
            worker_id=worker_id,
            storage_bucket="test-surveys",
            storage_prefix=prefix,
            manifest_key=f"{prefix}/manifest.json",
        )

    await survey_pool.execute("DELETE FROM auth.users WHERE id = 42")
    cleanup = await survey_pool.fetchrow(
        "SELECT * FROM scholight.survey_artifact_cleanup_outbox WHERE source_job_id = $1",
        job.id,
    )

    assert await survey_pool.fetchval("SELECT count(*) FROM scholight.surveys") == 0
    assert await survey_pool.fetchval("SELECT count(*) FROM scholight.survey_jobs") == 0
    assert cleanup is not None
    assert cleanup["user_id_snapshot"] == 42
    assert cleanup["bucket"] == "test-surveys"
    assert cleanup["storage_prefix"] == prefix


@pytest.mark.asyncio
async def test_progress_snapshot_advances_monotonically_and_is_owner_scoped(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="d" * 64,
        )
        queued = await get_survey_progress(survey_id=survey_id, user_id=42)
        worker_id = uuid4()
        job = await claim_survey_job(worker_id=worker_id, lease_seconds=3600)
        assert job is not None
        assert await update_survey_job_progress(
            job_id=job.id,
            worker_id=worker_id,
            stage="reviewing_evidence",
        )
        assert not await update_survey_job_progress(
            job_id=job.id,
            worker_id=worker_id,
            stage="discovering",
        )
        running = await get_survey_progress(survey_id=survey_id, user_id=42)
        hidden = await get_survey_progress(survey_id=survey_id, user_id=999)

    assert queued is not None
    assert queued.execution_stage == "waiting"
    assert queued.queue_kind == "survey"
    assert queued.queue_position == 1
    assert running is not None
    assert running.execution_stage == "reviewing_evidence"
    assert running.queue_position is None
    assert hidden is None


@pytest.mark.asyncio
async def test_missing_workspace_does_not_reverse_successful_execution_quota(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="d" * 64,
        )
        worker_id = uuid4()
        job = await claim_survey_job(worker_id=worker_id, lease_seconds=3600)
        assert job is not None
        await settle_survey_execution(
            job_id=job.id,
            worker_id=worker_id,
            outcome="succeeded",
            error_code=None,
            error_message=None,
        )
        await mark_survey_workspace_missing(job_id=job.id, worker_id=worker_id)
        await finish_survey_archive(
            job_id=job.id,
            worker_id=worker_id,
            storage_bucket="test-surveys",
            storage_prefix="surveys/v1/42/test",
            manifest_key="surveys/v1/42/test/manifest.json",
        )
        survey = await get_survey(survey_id=survey_id, user_id=42)

    assert survey is not None
    assert survey.status == "failed"
    assert survey.quota_state == "consumed"
    assert await _usage(survey_pool) == (0, 1)


@pytest.mark.asyncio
async def test_cancel_retains_drafts_and_releases_reservation_once(
    survey_pool: asyncpg.Pool,
) -> None:
    with patch("scholight.db.queries_survey.get_pool", return_value=survey_pool):
        survey_id = await _create()
        first = await cancel_survey(survey_id=survey_id, user_id=42)
        second = await cancel_survey(survey_id=survey_id, user_id=42)

    assert first.status == second.status == "cancelled"
    assert await _usage(survey_pool) == (0, 0)
    assert await survey_pool.fetchval("SELECT count(*) FROM scholight.survey_drafts") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["succeeded", "failed"])
async def test_cancel_terminal_survey_is_idempotent(
    survey_pool: asyncpg.Pool,
    terminal_status: str,
) -> None:
    with patch("scholight.db.queries_survey.get_pool", return_value=survey_pool):
        survey_id = await _create()
        await survey_pool.execute(
            "UPDATE scholight.surveys SET status = $2, quota_state = $3, "
            "finished_at = now() WHERE id = $1",
            survey_id,
            terminal_status,
            "consumed" if terminal_status == "succeeded" else "released",
        )
        unchanged = await cancel_survey(survey_id=survey_id, user_id=42)

    assert unchanged.status == terminal_status


@pytest.mark.asyncio
async def test_running_cancel_is_observed_and_wins_before_success_settlement(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="c" * 64,
        )
        worker_id = uuid4()
        job = await claim_survey_job(worker_id=worker_id, lease_seconds=3600)
        assert job is not None
        requested = await cancel_survey(survey_id=survey_id, user_id=42)
        heartbeat = await heartbeat_survey_job(
            job_id=job.id,
            worker_id=worker_id,
            lease_seconds=3600,
        )
        settled = await settle_survey_execution(
            job_id=job.id,
            worker_id=worker_id,
            outcome="succeeded",
            error_code=None,
            error_message=None,
        )
        await finish_survey_archive(
            job_id=job.id,
            worker_id=worker_id,
            storage_bucket="test-surveys",
            storage_prefix="surveys/v1/42/cancelled",
            manifest_key="surveys/v1/42/cancelled/manifest.json",
        )

    assert requested.status == "running"
    assert heartbeat == "cancel_requested"
    assert settled.terminal_outcome == "cancelled"
    assert (
        await survey_pool.fetchval(
            "SELECT count(*) FROM scholight.survey_email_notifications WHERE survey_id = $1",
            survey_id,
        )
        == 0
    )
    assert await _usage(survey_pool) == (0, 0)


@pytest.mark.asyncio
async def test_delete_drafting_survey_releases_quota_and_removes_aggregate(
    survey_pool: asyncpg.Pool,
) -> None:
    with patch("scholight.db.queries_survey.get_pool", return_value=survey_pool):
        survey_id = await _create()
        await delete_survey(survey_id=survey_id, user_id=42)

    assert (
        await survey_pool.fetchval(
            "SELECT 1 FROM scholight.surveys WHERE id = $1 AND user_id = $2",
            survey_id,
            42,
        )
        is None
    )
    assert await _usage(survey_pool) == (0, 0)


@pytest.mark.asyncio
async def test_delete_terminal_survey_enqueues_exact_artifact_cleanup(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="d" * 64,
        )
        worker_id = uuid4()
        job = await claim_survey_job(worker_id=worker_id, lease_seconds=3600)
        assert job is not None
        await settle_survey_execution(
            job_id=job.id,
            worker_id=worker_id,
            outcome="succeeded",
            error_code=None,
            error_message=None,
        )
        prefix = f"surveys/v1/42/{job.id}"
        await finish_survey_archive(
            job_id=job.id,
            worker_id=worker_id,
            storage_bucket="test-surveys",
            storage_prefix=prefix,
            manifest_key=f"{prefix}/manifest.json",
        )
        await delete_survey(survey_id=survey_id, user_id=42)

    cleanup = await survey_pool.fetchrow(
        "SELECT * FROM scholight.survey_artifact_cleanup_outbox WHERE source_job_id = $1",
        job.id,
    )
    assert cleanup is not None
    assert cleanup["storage_prefix"] == prefix
    with patch("scholight.db.queries_survey_cleanup.get_pool", return_value=survey_pool):
        status = await get_artifact_cleanup_status()
    assert status.pending == 1
    assert status.retry == 0
    assert status.dead == 0
    assert status.oldest_waiting_at is not None
    assert (
        await survey_pool.fetchval(
            "SELECT count(*) FROM scholight.surveys WHERE id = $1", survey_id
        )
        == 0
    )


@pytest.mark.asyncio
async def test_delete_running_or_archiving_survey_is_rejected(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="d" * 64,
        )
        assert await claim_survey_job(worker_id=uuid4(), lease_seconds=3600) is not None
        with pytest.raises(SurveyStateError) as error:
            await delete_survey(survey_id=survey_id, user_id=42)

    assert error.value.code == "survey_delete_in_progress"


@pytest.mark.asyncio
async def test_aggregate_list_and_artifact_reference_are_owner_scoped(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_views.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        page = await list_survey_summaries(
            user_id=42,
            quota_date=_QUOTA_DATE,
            daily_limit=5,
            view="active",
            limit=20,
            cursor_created_at=None,
            cursor_id=None,
        )
        hidden = await get_survey_artifact_reference(survey_id=survey_id, user_id=43)

    assert len(page.items) == 1
    assert page.items[0].id == survey_id
    assert page.items[0].progress.queue_kind == "draft"
    assert page.quota.remaining == 4
    assert hidden is None


@pytest.mark.asyncio
async def test_draft_claim_is_fair_and_respects_per_user_limit(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        await _create(user_id=42)
        await _create(user_id=42)
        await _create(user_id=43)
        first = await claim_survey_draft(
            worker_id=uuid4(), lease_seconds=3600, per_user_concurrency=1
        )
        second = await claim_survey_draft(
            worker_id=uuid4(), lease_seconds=3600, per_user_concurrency=1
        )
        blocked = await claim_survey_draft(
            worker_id=uuid4(), lease_seconds=3600, per_user_concurrency=1
        )

    assert first is not None and first.user_id == 42
    assert second is not None and second.user_id == 43
    assert blocked is None


@pytest.mark.asyncio
async def test_formal_claim_is_fair_and_respects_per_user_limit(
    survey_pool: asyncpg.Pool,
) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_ids = [
            (await _create(user_id=42), 42),
            (await _create(user_id=42), 42),
            (await _create(user_id=43), 43),
        ]
        for revision in range(1, 4):
            await _complete_next_draft(markdown=f"# Approved Draft {revision}")
        for survey_id, user_id in survey_ids:
            await start_survey(
                survey_id=survey_id,
                user_id=user_id,
                job_id=uuid4(),
                client_request_id=uuid4(),
                request_hash="f" * 64,
            )

        first = await claim_survey_job(
            worker_id=uuid4(), lease_seconds=3600, per_user_concurrency=1
        )
        second = await claim_survey_job(
            worker_id=uuid4(), lease_seconds=3600, per_user_concurrency=1
        )
        blocked = await claim_survey_job(
            worker_id=uuid4(), lease_seconds=3600, per_user_concurrency=1
        )

    assert first is not None and first.user_id == 42
    assert second is not None and second.user_id == 43
    assert blocked is None


@pytest.mark.asyncio
async def test_128_concurrent_draft_claims_enforce_global_and_user_limits(
    survey_pool: asyncpg.Pool,
) -> None:
    user_ids = tuple(range(1_000, 1_008))
    await survey_pool.executemany(
        "INSERT INTO auth.users (id, email_verified_at) VALUES ($1, now())",
        ((user_id,) for user_id in user_ids),
    )
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_capacity.get_pool", return_value=survey_pool),
    ):
        for user_id in user_ids:
            for _ in range(16):
                await _create(user_id=user_id, daily_limit=16)
        claimed = await asyncio.gather(
            *(
                claim_survey_draft(
                    worker_id=uuid4(),
                    lease_seconds=3600,
                    global_concurrency=64,
                    per_user_concurrency=8,
                )
                for _ in range(128)
            )
        )
        capacity = await get_survey_capacity_snapshot(queue="draft", per_user_concurrency=8)

    active = [draft for draft in claimed if draft is not None]
    counts: dict[int, int] = {}
    for draft in active:
        counts[draft.user_id] = counts.get(draft.user_id, 0) + 1
    assert len(active) == 64
    assert set(counts.values()) == {8}
    assert (capacity.queued, capacity.running, capacity.outstanding) == (64, 64, 128)
    assert capacity.users_at_limit == 8


@pytest.mark.asyncio
async def test_64_concurrent_full_claims_enforce_global_and_user_limits(
    survey_pool: asyncpg.Pool,
) -> None:
    user_ids = tuple(range(2_000, 2_004))
    await survey_pool.executemany(
        "INSERT INTO auth.users (id, email_verified_at) VALUES ($1, now())",
        ((user_id,) for user_id in user_ids),
    )
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_capacity.get_pool", return_value=survey_pool),
    ):
        surveys: list[tuple[UUID, int]] = []
        for user_id in user_ids:
            for _ in range(16):
                surveys.append((await _create(user_id=user_id, daily_limit=16), user_id))
        for revision in range(64):
            await _complete_next_draft(markdown=f"# Approved Draft {revision}")
        for survey_id, user_id in surveys:
            await start_survey(
                survey_id=survey_id,
                user_id=user_id,
                job_id=uuid4(),
                client_request_id=uuid4(),
                request_hash="a" * 64,
            )
        claimed = await asyncio.gather(
            *(
                claim_survey_job(
                    worker_id=uuid4(),
                    lease_seconds=3600,
                    global_concurrency=16,
                    per_user_concurrency=4,
                )
                for _ in range(64)
            )
        )
        capacity = await get_survey_capacity_snapshot(queue="survey", per_user_concurrency=4)

    active = [job for job in claimed if job is not None]
    counts: dict[int, int] = {}
    for job in active:
        counts[job.user_id] = counts.get(job.user_id, 0) + 1
    assert len(active) == 16
    assert set(counts.values()) == {4}
    assert (capacity.queued, capacity.running, capacity.outstanding) == (48, 16, 64)
    assert capacity.users_at_limit == 4


@pytest.mark.asyncio
async def test_cancel_and_claim_do_not_deadlock(survey_pool: asyncpg.Pool) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="c" * 64,
        )
        results = await asyncio.wait_for(
            asyncio.gather(
                cancel_survey(survey_id=survey_id, user_id=42),
                claim_survey_job(worker_id=uuid4(), lease_seconds=3600),
                return_exceptions=True,
            ),
            timeout=3,
        )

    assert not any(isinstance(result, asyncpg.LockNotAvailableError) for result in results)
    assert any(result is None or not isinstance(result, Exception) for result in results)


@pytest.mark.asyncio
async def test_cancel_and_complete_draft_do_not_deadlock(survey_pool: asyncpg.Pool) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        worker_id = uuid4()
        draft = await claim_survey_draft(worker_id=worker_id, lease_seconds=3600)
        assert draft is not None
        results = await asyncio.wait_for(
            asyncio.gather(
                cancel_survey(survey_id=survey_id, user_id=42),
                complete_survey_draft(
                    draft_id=draft.id,
                    worker_id=worker_id,
                    markdown="# Complete",
                ),
                return_exceptions=True,
            ),
            timeout=3,
        )

    assert not any(isinstance(result, asyncpg.LockNotAvailableError) for result in results)
    assert all(
        not isinstance(result, Exception) or isinstance(result, SurveyLeaseLostError)
        for result in results
    )


@pytest.mark.asyncio
async def test_start_and_claim_do_not_deadlock(survey_pool: asyncpg.Pool) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        results = await asyncio.wait_for(
            asyncio.gather(
                start_survey(
                    survey_id=survey_id,
                    user_id=42,
                    job_id=uuid4(),
                    client_request_id=uuid4(),
                    request_hash="d" * 64,
                ),
                claim_survey_job(worker_id=uuid4(), lease_seconds=3600),
                return_exceptions=True,
            ),
            timeout=3,
        )

    assert not any(isinstance(result, Exception) for result in results)


@pytest.mark.asyncio
async def test_settle_and_cancel_do_not_deadlock(survey_pool: asyncpg.Pool) -> None:
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=survey_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=survey_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="e" * 64,
        )
        worker_id = uuid4()
        job = await claim_survey_job(worker_id=worker_id, lease_seconds=3600)
        assert job is not None
        results = await asyncio.wait_for(
            asyncio.gather(
                settle_survey_execution(
                    job_id=job.id,
                    worker_id=worker_id,
                    outcome="failed",
                    error_code="survey_runtime_unavailable",
                    error_message="The Survey runtime did not complete successfully.",
                ),
                cancel_survey(survey_id=survey_id, user_id=42),
                return_exceptions=True,
            ),
            timeout=3,
        )

    assert isinstance(results[1], SurveyStateError)
    assert not isinstance(results[0], Exception)


@pytest.mark.asyncio
async def test_expired_running_job_recovers_after_database_pool_restart() -> None:
    database_url = isolated_database_url()
    first_pool = await asyncpg.create_pool(database_url, min_size=1, max_size=4)
    await reset_ingestion_database(first_pool)
    await first_pool.execute("INSERT INTO auth.users (id) VALUES (42)")
    with (
        patch("scholight.db.queries_survey.get_pool", return_value=first_pool),
        patch("scholight.db.queries_survey_drafts.get_pool", return_value=first_pool),
    ):
        survey_id = await _create()
        await _complete_next_draft(markdown="# Approved Draft")
        await start_survey(
            survey_id=survey_id,
            user_id=42,
            job_id=uuid4(),
            client_request_id=uuid4(),
            request_hash="d" * 64,
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
        assert archive_job is not None
        assert archive_job.id == claimed.id
        assert archive_job.status == "archiving"
        assert archive_job.terminal_outcome == "failed"
        assert archive_job.error_code == "survey_worker_lost"
    finally:
        await restarted_pool.close()
