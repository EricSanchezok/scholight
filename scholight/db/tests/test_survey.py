"""Survey reservation and durable state-transition tests."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from scholight.db.queries_survey import (
    SurveyQuotaExceededError,
    create_survey_job,
    settle_survey_execution,
)


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
    ) -> None:
        return None


def _job_row(
    *,
    job_id: UUID,
    worker_id: UUID | None = None,
    status: str = "pending",
    terminal_outcome: str | None = None,
) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "id": job_id,
        "user_id": 42,
        "topic": "retrieval augmented generation",
        "status": status,
        "terminal_outcome": terminal_outcome,
        "quota_date": date(2026, 7, 31),
        "storage_prefix": None,
        "manifest_key": None,
        "error_code": None,
        "error_message": None,
        "lease_owner": worker_id,
        "lease_expires_at": now if worker_id else None,
        "heartbeat_at": now if worker_id else None,
        "archive_attempts": 0,
        "next_archive_at": None,
        "created_at": now,
        "started_at": now if status != "pending" else None,
        "finished_at": None,
    }


def _pool_with_connection(connection: MagicMock) -> MagicMock:
    connection.transaction.return_value = _AsyncContext(MagicMock())
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(connection)
    return pool


@pytest.mark.asyncio
async def test_create_job_reserves_quota_in_same_transaction() -> None:
    job_id = uuid4()
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.fetchrow = AsyncMock(
        side_effect=[
            {"reserved_count": 1, "succeeded_count": 2},
            _job_row(job_id=job_id),
        ]
    )

    with patch(
        "scholight.db.queries_survey.get_pool",
        return_value=_pool_with_connection(connection),
    ):
        job = await create_survey_job(
            job_id=job_id,
            user_id=42,
            topic="retrieval augmented generation",
            quota_date=date(2026, 7, 31),
            daily_limit=5,
        )

    assert job.id == job_id
    assert any(
        "reserved_count = reserved_count + 1" in call.args[0]
        for call in connection.execute.await_args_list
    )


@pytest.mark.asyncio
async def test_create_job_rejects_sixth_reserved_or_successful_run() -> None:
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.fetchrow = AsyncMock(return_value={"reserved_count": 2, "succeeded_count": 3})

    with (
        patch(
            "scholight.db.queries_survey.get_pool",
            return_value=_pool_with_connection(connection),
        ),
        pytest.raises(SurveyQuotaExceededError),
    ):
        await create_survey_job(
            job_id=uuid4(),
            user_id=42,
            topic="retrieval augmented generation",
            quota_date=date(2026, 7, 31),
            daily_limit=5,
        )

    assert not any(
        "INSERT INTO scholight.survey_jobs" in call.args[0]
        for call in connection.fetchrow.await_args_list
    )


@pytest.mark.asyncio
async def test_success_settlement_moves_reservation_to_success_once() -> None:
    job_id = uuid4()
    worker_id = uuid4()
    running = _job_row(job_id=job_id, worker_id=worker_id, status="running")
    archiving = {
        **running,
        "status": "archiving",
        "terminal_outcome": "succeeded",
    }
    connection = MagicMock()
    connection.fetchrow = AsyncMock(side_effect=[running, {"reserved_count": 0}, archiving])

    with patch(
        "scholight.db.queries_survey.get_pool",
        return_value=_pool_with_connection(connection),
    ):
        job = await settle_survey_execution(
            job_id=job_id,
            worker_id=worker_id,
            outcome="succeeded",
            error_code=None,
            error_message=None,
        )

    quota_update = connection.fetchrow.await_args_list[1]
    assert quota_update.args[3] == 1
    assert job.status == "archiving"
    assert job.terminal_outcome == "succeeded"


@pytest.mark.asyncio
async def test_archiving_settlement_is_idempotent() -> None:
    job_id = uuid4()
    worker_id = uuid4()
    archiving = _job_row(
        job_id=job_id,
        worker_id=worker_id,
        status="archiving",
        terminal_outcome="failed",
    )
    connection = MagicMock()
    connection.fetchrow = AsyncMock(return_value=archiving)

    with patch(
        "scholight.db.queries_survey.get_pool",
        return_value=_pool_with_connection(connection),
    ):
        job = await settle_survey_execution(
            job_id=job_id,
            worker_id=worker_id,
            outcome="failed",
            error_code="survey_execution_failed",
            error_message="Survey generation failed.",
        )

    assert job.status == "archiving"
    connection.fetchrow.assert_awaited_once()
