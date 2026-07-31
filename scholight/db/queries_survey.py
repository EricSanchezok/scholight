"""Durable Scholight Survey jobs and daily-success quota transactions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool

logger = structlog.get_logger(__name__)

SurveyStatus = Literal["pending", "running", "archiving", "succeeded", "failed"]
SurveyOutcome = Literal["succeeded", "failed"]


class SurveyQuotaExceededError(DBError):
    """The user has no free reservation in today's successful-run quota."""


class SurveyJobStateError(DBError):
    """The requested state transition is no longer valid."""


@dataclass(frozen=True, slots=True)
class SurveyJob:
    id: UUID
    user_id: int
    topic: str
    status: SurveyStatus
    terminal_outcome: SurveyOutcome | None
    quota_date: date
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


def _job(row: asyncpg.Record | dict[str, Any]) -> SurveyJob:
    return SurveyJob(
        id=row["id"],
        user_id=int(row["user_id"]),
        topic=str(row["topic"]),
        status=row["status"],
        terminal_outcome=row["terminal_outcome"],
        quota_date=row["quota_date"],
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


async def create_survey_job(
    *,
    job_id: UUID,
    user_id: int,
    topic: str,
    quota_date: date,
    daily_limit: int,
) -> SurveyJob:
    """Reserve one daily slot and create the job in the same transaction."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO scholight.survey_daily_usage (user_id, usage_date) "
                "VALUES ($1, $2) ON CONFLICT (user_id, usage_date) DO NOTHING",
                user_id,
                quota_date,
            )
            usage = await connection.fetchrow(
                "SELECT reserved_count, succeeded_count "
                "FROM scholight.survey_daily_usage "
                "WHERE user_id = $1 AND usage_date = $2 FOR UPDATE",
                user_id,
                quota_date,
            )
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
                "INSERT INTO scholight.survey_jobs "
                "(id, user_id, topic, quota_date) VALUES ($1, $2, $3, $4) RETURNING *",
                job_id,
                user_id,
                topic,
                quota_date,
            )
            if row is None:
                raise DBError("Survey job was not created")
            return _job(row)
    except SurveyQuotaExceededError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_job_create_failed", error_type=type(exc).__name__)
        raise DBError("Failed to create Survey job") from exc


async def get_survey_job(*, job_id: UUID, user_id: int) -> SurveyJob | None:
    """Return one owner-scoped job."""
    try:
        row = await get_pool().fetchrow(
            "SELECT * FROM scholight.survey_jobs WHERE id = $1 AND user_id = $2",
            job_id,
            user_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_job_read_failed", error_type=type(exc).__name__)
        raise DBError("Failed to read Survey job") from exc
    return _job(row) if row is not None else None


async def list_survey_jobs(*, user_id: int, limit: int = 50) -> list[SurveyJob]:
    """Return the user's newest jobs without exposing another owner's work."""
    try:
        rows = await get_pool().fetch(
            "SELECT * FROM scholight.survey_jobs "
            "WHERE user_id = $1 ORDER BY created_at DESC, id DESC LIMIT $2",
            user_id,
            limit,
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_jobs_list_failed", error_type=type(exc).__name__)
        raise DBError("Failed to list Survey jobs") from exc
    return [_job(row) for row in rows]


async def claim_survey_job(*, worker_id: UUID, lease_seconds: int) -> SurveyJob | None:
    """Claim archive recovery first, then one new execution."""
    lease = timedelta(seconds=lease_seconds)
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT * FROM scholight.survey_jobs "
                "WHERE status = 'archiving' "
                "AND (next_archive_at IS NULL OR next_archive_at <= now()) "
                "AND (lease_expires_at IS NULL OR lease_expires_at <= now()) "
                "ORDER BY created_at, id FOR UPDATE SKIP LOCKED LIMIT 1"
            )
            if row is not None:
                claimed = await connection.fetchrow(
                    "UPDATE scholight.survey_jobs "
                    "SET lease_owner = $2, lease_expires_at = now() + $3, heartbeat_at = now() "
                    "WHERE id = $1 RETURNING *",
                    row["id"],
                    worker_id,
                    lease,
                )
                return _job(claimed)

            row = await connection.fetchrow(
                "SELECT * FROM scholight.survey_jobs "
                "WHERE status = 'pending' ORDER BY created_at, id "
                "FOR UPDATE SKIP LOCKED LIMIT 1"
            )
            if row is None:
                return None
            claimed = await connection.fetchrow(
                "UPDATE scholight.survey_jobs "
                "SET status = 'running', lease_owner = $2, "
                "lease_expires_at = now() + $3, heartbeat_at = now(), "
                "started_at = COALESCE(started_at, now()) "
                "WHERE id = $1 RETURNING *",
                row["id"],
                worker_id,
                lease,
            )
            return _job(claimed)
    except asyncpg.PostgresError as exc:
        logger.error("survey_job_claim_failed", error_type=type(exc).__name__)
        raise DBError("Failed to claim Survey job") from exc


async def heartbeat_survey_job(*, job_id: UUID, worker_id: UUID, lease_seconds: int) -> bool:
    """Extend one worker-owned running or archiving lease."""
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_jobs "
            "SET heartbeat_at = now(), lease_expires_at = now() + $3 "
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
    """Settle the reservation exactly once and enter artifact archiving."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT * FROM scholight.survey_jobs WHERE id = $1 FOR UPDATE",
                job_id,
            )
            if row is None:
                raise SurveyJobStateError("Survey job no longer exists")
            if row["status"] == "archiving" and row["terminal_outcome"] == outcome:
                return _job(row)
            if row["status"] != "running" or row["lease_owner"] != worker_id:
                raise SurveyJobStateError("Survey job execution lease is no longer owned")
            succeeded_delta = 1 if outcome == "succeeded" else 0
            usage = await connection.fetchrow(
                "UPDATE scholight.survey_daily_usage "
                "SET reserved_count = reserved_count - 1, "
                "succeeded_count = succeeded_count + $3, updated_at = now() "
                "WHERE user_id = $1 AND usage_date = $2 AND reserved_count > 0 "
                "RETURNING reserved_count",
                row["user_id"],
                row["quota_date"],
                succeeded_delta,
            )
            if usage is None:
                raise SurveyJobStateError("Survey reservation is missing")
            archived = await connection.fetchrow(
                "UPDATE scholight.survey_jobs "
                "SET status = 'archiving', terminal_outcome = $2, "
                "error_code = $3, error_message = $4, next_archive_at = now(), "
                "heartbeat_at = now() WHERE id = $1 RETURNING *",
                job_id,
                outcome,
                error_code,
                error_message,
            )
            return _job(archived)
    except SurveyJobStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_execution_settle_failed", error_type=type(exc).__name__)
        raise DBError("Failed to settle Survey execution") from exc


async def finish_survey_archive(
    *,
    job_id: UUID,
    worker_id: UUID,
    storage_prefix: str,
    manifest_key: str,
) -> SurveyJob:
    """Finalize an already-settled job after manifest verification."""
    try:
        row = await get_pool().fetchrow(
            "UPDATE scholight.survey_jobs "
            "SET status = terminal_outcome, storage_prefix = $3, manifest_key = $4, "
            "lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = now(), "
            "next_archive_at = NULL, finished_at = now() "
            "WHERE id = $1 AND lease_owner = $2 AND status = 'archiving' "
            "RETURNING *",
            job_id,
            worker_id,
            storage_prefix,
            manifest_key,
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_archive_finish_failed", error_type=type(exc).__name__)
        raise DBError("Failed to finalize Survey archive") from exc
    if row is None:
        raise SurveyJobStateError("Survey archive lease is no longer owned")
    return _job(row)


async def defer_survey_archive(
    *,
    job_id: UUID,
    worker_id: UUID,
    retry_after: timedelta,
    error_code: str,
    error_message: str,
) -> bool:
    """Release an archive lease for a bounded, non-execution retry."""
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_jobs "
            "SET archive_attempts = archive_attempts + 1, "
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
    """Convert an unarchivable success to failure without double-charging success quota."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT * FROM scholight.survey_jobs "
                "WHERE id = $1 AND lease_owner = $2 AND status = 'archiving' FOR UPDATE",
                job_id,
                worker_id,
            )
            if row is None:
                raise SurveyJobStateError("Survey archive lease is no longer owned")
            if row["terminal_outcome"] == "succeeded":
                usage = await connection.fetchrow(
                    "UPDATE scholight.survey_daily_usage "
                    "SET succeeded_count = succeeded_count - 1, updated_at = now() "
                    "WHERE user_id = $1 AND usage_date = $2 AND succeeded_count > 0 "
                    "RETURNING succeeded_count",
                    row["user_id"],
                    row["quota_date"],
                )
                if usage is None:
                    raise SurveyJobStateError("Survey success settlement is missing")
            failed = await connection.fetchrow(
                "UPDATE scholight.survey_jobs "
                "SET terminal_outcome = 'failed', error_code = 'survey_workspace_missing', "
                "error_message = 'The Survey workspace was unavailable during archiving.' "
                "WHERE id = $1 RETURNING *",
                job_id,
            )
            return _job(failed)
    except SurveyJobStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_workspace_missing_update_failed", error_type=type(exc).__name__)
        raise DBError("Failed to record missing Survey workspace") from exc


async def recover_expired_survey_jobs(*, limit: int = 20) -> int:
    """Fail expired executions, release quota, and preserve work for archiving."""
    recovered = 0
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                "SELECT * FROM scholight.survey_jobs "
                "WHERE status = 'running' AND lease_expires_at <= now() "
                "ORDER BY lease_expires_at FOR UPDATE SKIP LOCKED LIMIT $1",
                limit,
            )
            for row in rows:
                usage = await connection.fetchrow(
                    "UPDATE scholight.survey_daily_usage "
                    "SET reserved_count = reserved_count - 1, updated_at = now() "
                    "WHERE user_id = $1 AND usage_date = $2 AND reserved_count > 0 "
                    "RETURNING reserved_count",
                    row["user_id"],
                    row["quota_date"],
                )
                if usage is None:
                    raise SurveyJobStateError("Expired Survey reservation is missing")
                await connection.execute(
                    "UPDATE scholight.survey_jobs "
                    "SET status = 'archiving', terminal_outcome = 'failed', "
                    "error_code = 'survey_worker_lost', "
                    "error_message = 'The Survey worker stopped before completion.', "
                    "lease_owner = NULL, lease_expires_at = NULL, "
                    "next_archive_at = now(), heartbeat_at = now() WHERE id = $1",
                    row["id"],
                )
                recovered += 1
    except SurveyJobStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_expired_recovery_failed", error_type=type(exc).__name__)
        raise DBError("Failed to recover expired Survey jobs") from exc
    return recovered


async def delete_pending_survey_job(*, job_id: UUID, user_id: int) -> bool:
    """Delete one pending job and release its reservation atomically."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT * FROM scholight.survey_jobs WHERE id = $1 AND user_id = $2 FOR UPDATE",
                job_id,
                user_id,
            )
            if row is None:
                return False
            if row["status"] != "pending":
                raise SurveyJobStateError("Only a pending Survey job can be cancelled")
            usage = await connection.fetchrow(
                "UPDATE scholight.survey_daily_usage "
                "SET reserved_count = reserved_count - 1, updated_at = now() "
                "WHERE user_id = $1 AND usage_date = $2 AND reserved_count > 0 "
                "RETURNING reserved_count",
                user_id,
                row["quota_date"],
            )
            if usage is None:
                raise SurveyJobStateError("Survey reservation is missing")
            await connection.execute(
                "DELETE FROM scholight.survey_jobs WHERE id = $1",
                job_id,
            )
            return True
    except SurveyJobStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("pending_survey_delete_failed", error_type=type(exc).__name__)
        raise DBError("Failed to cancel Survey job") from exc


async def delete_terminal_survey_job(*, job_id: UUID, user_id: int) -> bool:
    """Delete a terminal row only after its exact artifact set was removed."""
    try:
        result = await get_pool().execute(
            "DELETE FROM scholight.survey_jobs "
            "WHERE id = $1 AND user_id = $2 AND status IN ('succeeded', 'failed')",
            job_id,
            user_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("terminal_survey_delete_failed", error_type=type(exc).__name__)
        raise DBError("Failed to delete Survey job") from exc
    return str(result) == "DELETE 1"


async def get_survey_usage(*, user_id: int, usage_date: date) -> tuple[int, int]:
    """Return reservation and success counts for the UTC date."""
    try:
        row = await get_pool().fetchrow(
            "SELECT reserved_count, succeeded_count "
            "FROM scholight.survey_daily_usage WHERE user_id = $1 AND usage_date = $2",
            user_id,
            usage_date,
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_usage_read_failed", error_type=type(exc).__name__)
        raise DBError("Failed to read Survey usage") from exc
    if row is None:
        return 0, 0
    return int(row["reserved_count"]), int(row["succeeded_count"])


async def get_survey_job_counts() -> dict[str, int]:
    """Return low-cardinality queue counts for operator status output."""
    counts = {
        "pending": 0,
        "running": 0,
        "archiving": 0,
        "succeeded": 0,
        "failed": 0,
    }
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
    "SurveyJob",
    "SurveyJobStateError",
    "SurveyOutcome",
    "SurveyQuotaExceededError",
    "claim_survey_job",
    "create_survey_job",
    "defer_survey_archive",
    "delete_pending_survey_job",
    "delete_terminal_survey_job",
    "finish_survey_archive",
    "get_survey_job",
    "get_survey_job_counts",
    "get_survey_usage",
    "heartbeat_survey_job",
    "list_survey_jobs",
    "mark_survey_workspace_missing",
    "recover_expired_survey_jobs",
    "settle_survey_execution",
]
