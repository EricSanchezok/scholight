"""Survey aggregate, quota, and formal execution transactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool

logger = structlog.get_logger(__name__)

SurveyStatus = Literal[
    "drafting", "queued", "running", "archiving", "succeeded", "failed", "cancelled"
]
QuotaState = Literal["reserved", "consumed", "released"]
JobStatus = Literal["queued", "running", "archiving", "finished"]
SurveyOutcome = Literal["succeeded", "failed"]


class SurveyQuotaExceededError(DBError):
    """The user has no free reservation in today's Survey allowance."""


class SurveyStateError(DBError):
    """The requested aggregate transition is no longer valid."""


@dataclass(frozen=True, slots=True)
class Survey:
    id: UUID
    user_id: int
    client_request_id: UUID
    initial_request: str
    status: SurveyStatus
    quota_date: date
    quota_state: QuotaState
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class SurveyJob:
    id: UUID
    survey_id: UUID
    user_id: int
    approved_draft_id: UUID
    approved_draft: str
    approved_draft_revision: int
    client_request_id: UUID
    status: JobStatus
    terminal_outcome: SurveyOutcome | None
    storage_prefix: str | None
    manifest_key: str | None
    error_code: str | None
    error_message: str | None
    lease_owner: UUID | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    archive_attempts: int
    next_archive_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


def _survey(row: asyncpg.Record | dict[str, Any]) -> Survey:
    return Survey(
        id=row["id"],
        user_id=int(row["user_id"]),
        client_request_id=row["client_request_id"],
        initial_request=str(row["initial_request"]),
        status=row["status"],
        quota_date=row["quota_date"],
        quota_state=row["quota_state"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def _job(row: asyncpg.Record | dict[str, Any]) -> SurveyJob:
    return SurveyJob(
        id=row["id"],
        survey_id=row["survey_id"],
        user_id=int(row["user_id"]),
        approved_draft_id=row["approved_draft_id"],
        approved_draft=str(row["approved_draft"]),
        approved_draft_revision=int(row["approved_draft_revision"]),
        client_request_id=row["client_request_id"],
        status=row["status"],
        terminal_outcome=row["terminal_outcome"],
        storage_prefix=row["storage_prefix"],
        manifest_key=row["manifest_key"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        heartbeat_at=row["heartbeat_at"],
        archive_attempts=int(row["archive_attempts"]),
        next_archive_at=row["next_archive_at"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


_JOB_SELECT = (
    "SELECT j.*, s.user_id, d.markdown AS approved_draft, "
    "d.revision AS approved_draft_revision FROM scholight.survey_jobs j "
    "JOIN scholight.surveys s ON s.id = j.survey_id "
    "JOIN scholight.survey_drafts d ON d.id = j.approved_draft_id "
)


async def create_survey(
    *,
    survey_id: UUID,
    draft_id: UUID,
    user_id: int,
    initial_request: str,
    client_request_id: UUID,
    quota_date: date,
    daily_limit: int,
) -> Survey:
    """Reserve quota and create the aggregate plus its first Draft atomically."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO scholight.survey_daily_usage (user_id, usage_date) "
                "VALUES ($1, $2) ON CONFLICT (user_id, usage_date) DO NOTHING",
                user_id,
                quota_date,
            )
            usage = await connection.fetchrow(
                "SELECT reserved_count, succeeded_count FROM scholight.survey_daily_usage "
                "WHERE user_id = $1 AND usage_date = $2 FOR UPDATE",
                user_id,
                quota_date,
            )
            existing = await connection.fetchrow(
                "SELECT * FROM scholight.surveys WHERE user_id = $1 AND client_request_id = $2",
                user_id,
                client_request_id,
            )
            if existing is not None:
                return _survey(existing)
            if usage is None:
                raise DBError("Survey quota row was not created")
            if int(usage["reserved_count"]) + int(usage["succeeded_count"]) >= daily_limit:
                raise SurveyQuotaExceededError("Daily Survey quota reached")
            await connection.execute(
                "UPDATE scholight.survey_daily_usage "
                "SET reserved_count = reserved_count + 1, updated_at = now() "
                "WHERE user_id = $1 AND usage_date = $2",
                user_id,
                quota_date,
            )
            row = await connection.fetchrow(
                "INSERT INTO scholight.surveys "
                "(id, user_id, client_request_id, initial_request, quota_date) "
                "VALUES ($1, $2, $3, $4, $5) RETURNING *",
                survey_id,
                user_id,
                client_request_id,
                initial_request,
                quota_date,
            )
            await connection.execute(
                "INSERT INTO scholight.survey_drafts "
                "(id, survey_id, client_request_id, source, user_message) "
                "VALUES ($1, $2, $3, 'generated', $4)",
                draft_id,
                survey_id,
                client_request_id,
                initial_request,
            )
            if row is None:
                raise DBError("Survey was not created")
            return _survey(row)
    except SurveyQuotaExceededError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_create_failed", error_type=type(exc).__name__)
        raise DBError("Failed to create Survey") from exc


async def get_survey(*, survey_id: UUID, user_id: int) -> Survey | None:
    try:
        row = await get_pool().fetchrow(
            "SELECT * FROM scholight.surveys WHERE id = $1 AND user_id = $2",
            survey_id,
            user_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_read_failed", error_type=type(exc).__name__)
        raise DBError("Failed to read Survey") from exc
    return _survey(row) if row is not None else None


async def list_surveys(*, user_id: int, limit: int = 50) -> list[Survey]:
    try:
        rows = await get_pool().fetch(
            "SELECT * FROM scholight.surveys WHERE user_id = $1 "
            "ORDER BY created_at DESC, id DESC LIMIT $2",
            user_id,
            limit,
        )
    except asyncpg.PostgresError as exc:
        logger.error("surveys_list_failed", error_type=type(exc).__name__)
        raise DBError("Failed to list Surveys") from exc
    return [_survey(row) for row in rows]


async def start_survey(
    *, survey_id: UUID, user_id: int, job_id: UUID, client_request_id: UUID
) -> Survey:
    """Bind the latest ready Draft and enqueue the only formal execution."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT * FROM scholight.surveys WHERE id = $1 AND user_id = $2 FOR UPDATE",
                survey_id,
                user_id,
            )
            if row is None:
                raise SurveyStateError("Survey not found")
            existing = await connection.fetchrow(
                "SELECT client_request_id FROM scholight.survey_jobs WHERE survey_id = $1",
                survey_id,
            )
            if existing is not None:
                if existing["client_request_id"] == client_request_id:
                    return _survey(row)
                raise SurveyStateError("Survey execution already exists")
            if row["status"] != "drafting" or row["quota_state"] != "reserved":
                raise SurveyStateError("Survey cannot be started from its current state")
            active = await connection.fetchval(
                "SELECT EXISTS (SELECT 1 FROM scholight.survey_drafts "
                "WHERE survey_id = $1 AND status IN ('queued', 'running'))",
                survey_id,
            )
            if active:
                raise SurveyStateError("Survey Draft is still being generated")
            draft = await connection.fetchrow(
                "SELECT id FROM scholight.survey_drafts "
                "WHERE survey_id = $1 AND status = 'ready' "
                "ORDER BY revision DESC LIMIT 1",
                survey_id,
            )
            if draft is None:
                raise SurveyStateError("Survey has no ready Draft")
            await connection.execute(
                "INSERT INTO scholight.survey_jobs "
                "(id, survey_id, approved_draft_id, client_request_id) VALUES ($1, $2, $3, $4)",
                job_id,
                survey_id,
                draft["id"],
                client_request_id,
            )
            updated = await connection.fetchrow(
                "UPDATE scholight.surveys SET status = 'queued', updated_at = now() "
                "WHERE id = $1 RETURNING *",
                survey_id,
            )
            return _survey(updated)
    except SurveyStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_start_failed", error_type=type(exc).__name__)
        raise DBError("Failed to start Survey") from exc


async def cancel_survey(*, survey_id: UUID, user_id: int) -> Survey:
    """Cancel only pre-execution work and release its reservation once."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT * FROM scholight.surveys WHERE id = $1 AND user_id = $2 FOR UPDATE",
                survey_id,
                user_id,
            )
            if row is None:
                raise SurveyStateError("Survey not found")
            if row["status"] == "cancelled":
                return _survey(row)
            if row["status"] not in {"drafting", "queued"}:
                raise SurveyStateError("Survey can no longer be cancelled")
            if row["status"] == "queued":
                deleted = await connection.execute(
                    "DELETE FROM scholight.survey_jobs WHERE survey_id = $1 AND status = 'queued'",
                    survey_id,
                )
                if str(deleted) != "DELETE 1":
                    raise SurveyStateError("Survey execution already started")
            await connection.execute(
                "UPDATE scholight.survey_drafts SET status = 'cancelled', finished_at = now(), "
                "lease_owner = NULL, lease_expires_at = NULL "
                "WHERE survey_id = $1 AND status IN ('queued', 'running')",
                survey_id,
            )
            usage = await connection.fetchrow(
                "UPDATE scholight.survey_daily_usage "
                "SET reserved_count = reserved_count - 1, updated_at = now() "
                "WHERE user_id = $1 AND usage_date = $2 AND reserved_count > 0 "
                "RETURNING reserved_count",
                user_id,
                row["quota_date"],
            )
            if usage is None:
                raise SurveyStateError("Survey reservation is missing")
            updated = await connection.fetchrow(
                "UPDATE scholight.surveys SET status = 'cancelled', quota_state = 'released', "
                "updated_at = now(), finished_at = now() WHERE id = $1 RETURNING *",
                survey_id,
            )
            return _survey(updated)
    except SurveyStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_cancel_failed", error_type=type(exc).__name__)
        raise DBError("Failed to cancel Survey") from exc


async def claim_survey_job(*, worker_id: UUID, lease_seconds: int) -> SurveyJob | None:
    lease = timedelta(seconds=lease_seconds)
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                _JOB_SELECT + "WHERE j.status = 'archiving' "
                "AND (j.next_archive_at IS NULL OR j.next_archive_at <= now()) "
                "AND (j.lease_expires_at IS NULL OR j.lease_expires_at <= now()) "
                "ORDER BY j.created_at, j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1"
            )
            if row is None:
                row = await connection.fetchrow(
                    _JOB_SELECT + "WHERE j.status = 'queued' ORDER BY j.created_at, j.id "
                    "FOR UPDATE OF j SKIP LOCKED LIMIT 1"
                )
                if row is None:
                    return None
                await connection.execute(
                    "UPDATE scholight.surveys SET status = 'running', started_at = now(), "
                    "updated_at = now() WHERE id = $1 AND status = 'queued'",
                    row["survey_id"],
                )
                row = await connection.fetchrow(
                    "UPDATE scholight.survey_jobs SET status = 'running', lease_owner = $2, "
                    "lease_expires_at = now() + $3, heartbeat_at = now(), started_at = now() "
                    "WHERE id = $1 RETURNING *",
                    row["id"],
                    worker_id,
                    lease,
                )
                row = await connection.fetchrow(_JOB_SELECT + "WHERE j.id = $1", row["id"])
            else:
                await connection.execute(
                    "UPDATE scholight.survey_jobs SET lease_owner = $2, "
                    "lease_expires_at = now() + $3, heartbeat_at = now() WHERE id = $1",
                    row["id"],
                    worker_id,
                    lease,
                )
                row = await connection.fetchrow(_JOB_SELECT + "WHERE j.id = $1", row["id"])
            return _job(row)
    except asyncpg.PostgresError as exc:
        logger.error("survey_job_claim_failed", error_type=type(exc).__name__)
        raise DBError("Failed to claim Survey job") from exc


async def heartbeat_survey_job(*, job_id: UUID, worker_id: UUID, lease_seconds: int) -> bool:
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_jobs SET heartbeat_at = now(), lease_expires_at = now() + $3 "
            "WHERE id = $1 AND lease_owner = $2 AND status IN ('running', 'archiving')",
            job_id,
            worker_id,
            timedelta(seconds=lease_seconds),
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_job_heartbeat_failed", error_type=type(exc).__name__)
        raise DBError("Failed to heartbeat Survey job") from exc
    return str(result) == "UPDATE 1"


async def settle_survey_execution(
    *,
    job_id: UUID,
    worker_id: UUID,
    outcome: SurveyOutcome,
    error_code: str | None,
    error_message: str | None,
) -> SurveyJob:
    """Settle quota once and enter archiving without creating another Survey."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(_JOB_SELECT + "WHERE j.id = $1 FOR UPDATE OF j", job_id)
            if row is None:
                raise SurveyStateError("Survey job no longer exists")
            if row["status"] == "archiving" and row["terminal_outcome"] == outcome:
                return _job(row)
            if row["status"] != "running" or row["lease_owner"] != worker_id:
                raise SurveyStateError("Survey execution lease is no longer owned")
            survey = await connection.fetchrow(
                "SELECT * FROM scholight.surveys WHERE id = $1 FOR UPDATE", row["survey_id"]
            )
            if (
                survey is None
                or survey["status"] != "running"
                or survey["quota_state"] != "reserved"
            ):
                raise SurveyStateError("Survey aggregate is not running")
            success_delta = 1 if outcome == "succeeded" else 0
            usage = await connection.fetchrow(
                "UPDATE scholight.survey_daily_usage SET reserved_count = reserved_count - 1, "
                "succeeded_count = succeeded_count + $3, updated_at = now() "
                "WHERE user_id = $1 AND usage_date = $2 AND reserved_count > 0 RETURNING 1",
                survey["user_id"],
                survey["quota_date"],
                success_delta,
            )
            if usage is None:
                raise SurveyStateError("Survey reservation is missing")
            await connection.execute(
                "UPDATE scholight.surveys SET status = 'archiving', quota_state = $2, "
                "error_code = $3, error_message = $4, updated_at = now() WHERE id = $1",
                row["survey_id"],
                "consumed" if outcome == "succeeded" else "released",
                error_code,
                error_message,
            )
            await connection.execute(
                "UPDATE scholight.survey_jobs SET status = 'archiving', terminal_outcome = $2, "
                "error_code = $3, error_message = $4, next_archive_at = now(), heartbeat_at = now() "
                "WHERE id = $1",
                job_id,
                outcome,
                error_code,
                error_message,
            )
            updated = await connection.fetchrow(_JOB_SELECT + "WHERE j.id = $1", job_id)
            return _job(updated)
    except SurveyStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_execution_settle_failed", error_type=type(exc).__name__)
        raise DBError("Failed to settle Survey execution") from exc


async def finish_survey_archive(
    *, job_id: UUID, worker_id: UUID, storage_prefix: str, manifest_key: str
) -> SurveyJob:
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                _JOB_SELECT + "WHERE j.id = $1 AND j.lease_owner = $2 "
                "AND j.status = 'archiving' FOR UPDATE OF j",
                job_id,
                worker_id,
            )
            if row is None:
                raise SurveyStateError("Survey archive lease is no longer owned")
            await connection.execute(
                "UPDATE scholight.survey_jobs SET status = 'finished', storage_prefix = $3, "
                "manifest_key = $4, lease_owner = NULL, lease_expires_at = NULL, "
                "heartbeat_at = now(), next_archive_at = NULL, finished_at = now() "
                "WHERE id = $1 AND lease_owner = $2",
                job_id,
                worker_id,
                storage_prefix,
                manifest_key,
            )
            await connection.execute(
                "UPDATE scholight.surveys SET status = $2, updated_at = now(), finished_at = now() "
                "WHERE id = $1 AND status = 'archiving'",
                row["survey_id"],
                row["terminal_outcome"],
            )
            updated = await connection.fetchrow(_JOB_SELECT + "WHERE j.id = $1", job_id)
            return _job(updated)
    except SurveyStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_archive_finish_failed", error_type=type(exc).__name__)
        raise DBError("Failed to finalize Survey archive") from exc


async def defer_survey_archive(
    *,
    job_id: UUID,
    worker_id: UUID,
    retry_after: timedelta,
    error_code: str,
    error_message: str,
) -> bool:
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_jobs SET archive_attempts = archive_attempts + 1, "
            "next_archive_at = now() + $3, error_code = $4, error_message = $5, "
            "lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = now() "
            "WHERE id = $1 AND lease_owner = $2 AND status = 'archiving'",
            job_id,
            worker_id,
            retry_after,
            error_code,
            error_message,
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_archive_defer_failed", error_type=type(exc).__name__)
        raise DBError("Failed to defer Survey archive") from exc
    return str(result) == "UPDATE 1"


async def mark_survey_workspace_missing(*, job_id: UUID, worker_id: UUID) -> SurveyJob:
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                _JOB_SELECT + "WHERE j.id = $1 AND j.lease_owner = $2 "
                "AND j.status = 'archiving' FOR UPDATE OF j",
                job_id,
                worker_id,
            )
            if row is None:
                raise SurveyStateError("Survey archive lease is no longer owned")
            await connection.execute(
                "UPDATE scholight.survey_jobs SET terminal_outcome = 'failed', "
                "error_code = 'survey_workspace_missing', "
                "error_message = 'The Survey workspace was unavailable during archiving.' "
                "WHERE id = $1",
                job_id,
            )
            await connection.execute(
                "UPDATE scholight.surveys SET error_code = 'survey_workspace_missing', "
                "error_message = 'The Survey workspace was unavailable during archiving.', "
                "updated_at = now() WHERE id = $1",
                row["survey_id"],
            )
            updated = await connection.fetchrow(_JOB_SELECT + "WHERE j.id = $1", job_id)
            return _job(updated)
    except SurveyStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_workspace_missing_update_failed", error_type=type(exc).__name__)
        raise DBError("Failed to record missing Survey workspace") from exc


async def recover_expired_survey_jobs(*, limit: int = 20) -> int:
    recovered = 0
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                _JOB_SELECT + "WHERE j.status = 'running' AND j.lease_expires_at <= now() "
                "ORDER BY j.lease_expires_at FOR UPDATE OF j SKIP LOCKED LIMIT $1",
                limit,
            )
            for row in rows:
                survey = await connection.fetchrow(
                    "SELECT * FROM scholight.surveys WHERE id = $1 FOR UPDATE", row["survey_id"]
                )
                usage = await connection.fetchrow(
                    "UPDATE scholight.survey_daily_usage SET reserved_count = reserved_count - 1, "
                    "updated_at = now() WHERE user_id = $1 AND usage_date = $2 "
                    "AND reserved_count > 0 RETURNING 1",
                    row["user_id"],
                    survey["quota_date"],
                )
                if usage is None:
                    raise SurveyStateError("Expired Survey reservation is missing")
                await connection.execute(
                    "UPDATE scholight.survey_jobs SET status = 'archiving', "
                    "terminal_outcome = 'failed', error_code = 'survey_worker_lost', "
                    "error_message = 'The Survey worker stopped before completion.', "
                    "lease_owner = NULL, lease_expires_at = NULL, next_archive_at = now(), "
                    "heartbeat_at = now() WHERE id = $1",
                    row["id"],
                )
                await connection.execute(
                    "UPDATE scholight.surveys SET status = 'archiving', quota_state = 'released', "
                    "error_code = 'survey_worker_lost', "
                    "error_message = 'The Survey worker stopped before completion.', "
                    "updated_at = now() WHERE id = $1",
                    row["survey_id"],
                )
                recovered += 1
    except SurveyStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_expired_recovery_failed", error_type=type(exc).__name__)
        raise DBError("Failed to recover expired Survey jobs") from exc
    return recovered


async def get_survey_job_counts() -> dict[str, int]:
    counts = {"queued": 0, "running": 0, "archiving": 0, "finished": 0}
    try:
        rows = await get_pool().fetch(
            "SELECT status, count(*) AS count FROM scholight.survey_jobs GROUP BY status"
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_job_counts_failed", error_type=type(exc).__name__)
        raise DBError("Failed to read Survey job counts") from exc
    for row in rows:
        counts[str(row["status"])] = int(row["count"])
    return counts


__all__ = [
    "Survey",
    "SurveyJob",
    "SurveyOutcome",
    "SurveyQuotaExceededError",
    "SurveyStateError",
    "cancel_survey",
    "claim_survey_job",
    "create_survey",
    "defer_survey_archive",
    "finish_survey_archive",
    "get_survey",
    "get_survey_job_counts",
    "heartbeat_survey_job",
    "list_surveys",
    "mark_survey_workspace_missing",
    "recover_expired_survey_jobs",
    "settle_survey_execution",
    "start_survey",
]
