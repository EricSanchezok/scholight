"""Survey aggregate reservation and formal execution transaction tests."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime
from types import TracebackType
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from scholight.db.queries_survey import (
    SurveyQuotaExceededError,
    create_survey,
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


def _survey_row(*, survey_id: UUID, status: str = "drafting") -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "id": survey_id,
        "user_id": 42,
        "client_request_id": uuid4(),
        "initial_request": "retrieval augmented generation",
        "status": status,
        "quota_date": date(2026, 7, 31),
        "quota_state": "reserved" if status not in {"succeeded", "failed"} else "released",
        "error_code": None,
        "error_message": None,
        "created_at": now,
        "updated_at": now,
        "started_at": now if status == "running" else None,
        "finished_at": None,
    }


def _job_row(
    *, job_id: UUID, survey_id: UUID, worker_id: UUID, status: str = "running"
) -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "id": job_id,
        "survey_id": survey_id,
        "user_id": 42,
        "approved_draft_id": uuid4(),
        "approved_draft": "# Research brief",
        "approved_draft_revision": 1,
        "client_request_id": uuid4(),
        "status": status,
        "terminal_outcome": None,
        "storage_prefix": None,
        "manifest_key": None,
        "error_code": None,
        "error_message": None,
        "lease_owner": worker_id,
        "lease_expires_at": now,
        "heartbeat_at": now,
        "progress_stage": "reviewing_evidence",
        "progress_updated_at": now,
        "archive_attempts": 0,
        "next_archive_at": None,
        "created_at": now,
        "started_at": now,
        "finished_at": None,
    }


def _pool_with_connection(connection: MagicMock) -> MagicMock:
    connection.transaction.return_value = _AsyncContext(MagicMock())
    pool = MagicMock()
    pool.acquire.return_value = _AsyncContext(connection)
    return pool


@pytest.mark.asyncio
async def test_create_survey_reserves_quota_and_queues_initial_draft_atomically() -> None:
    survey_id = uuid4()
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.fetchrow = AsyncMock(
        side_effect=[
            {"reserved_count": 1, "succeeded_count": 2},
            None,
            _survey_row(survey_id=survey_id),
        ]
    )

    with patch(
        "scholight.db.queries_survey.get_pool",
        return_value=_pool_with_connection(connection),
    ):
        survey = await create_survey(
            survey_id=survey_id,
            draft_id=uuid4(),
            user_id=42,
            initial_request="retrieval augmented generation",
            client_request_id=uuid4(),
            quota_date=date(2026, 7, 31),
            daily_limit=5,
        )

    assert survey.id == survey_id
    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert any("reserved_count = reserved_count + 1" in sql for sql in statements)
    assert any("INSERT INTO scholight.survey_drafts" in sql for sql in statements)


@pytest.mark.asyncio
async def test_create_survey_rejects_sixth_reserved_or_successful_run() -> None:
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.fetchrow = AsyncMock(side_effect=[{"reserved_count": 2, "succeeded_count": 3}, None])

    with (
        patch(
            "scholight.db.queries_survey.get_pool",
            return_value=_pool_with_connection(connection),
        ),
        pytest.raises(SurveyQuotaExceededError),
    ):
        await create_survey(
            survey_id=uuid4(),
            draft_id=uuid4(),
            user_id=42,
            initial_request="retrieval augmented generation",
            client_request_id=uuid4(),
            quota_date=date(2026, 7, 31),
            daily_limit=5,
        )

    assert not any(
        "INSERT INTO scholight.surveys" in call.args[0]
        for call in connection.fetchrow.await_args_list
    )


@pytest.mark.asyncio
async def test_failed_execution_releases_reservation_without_creating_another_survey() -> None:
    survey_id = uuid4()
    job_id = uuid4()
    worker_id = uuid4()
    running_job = _job_row(job_id=job_id, survey_id=survey_id, worker_id=worker_id)
    archiving_job = {
        **running_job,
        "status": "archiving",
        "terminal_outcome": "failed",
        "error_code": "survey_execution_failed",
    }
    connection = MagicMock()
    connection.execute = AsyncMock()
    connection.fetchrow = AsyncMock(
        side_effect=[
            running_job,
            _survey_row(survey_id=survey_id, status="running"),
            {"settled": 1},
            archiving_job,
        ]
    )

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
    assert job.terminal_outcome == "failed"
    statements = [call.args[0] for call in connection.execute.await_args_list]
    assert all("INSERT INTO scholight.surveys" not in sql for sql in statements)
