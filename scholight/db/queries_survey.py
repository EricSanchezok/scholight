"""Survey aggregate, quota, and formal execution transactions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool
from scholight.db.survey_locking import lock_survey_aggregate, lock_survey_capacity
from scholight.survey.contracts import (
    HeartbeatState,
    SurveyConflictError,
    SurveyLeaseLostError,
)
from scholight.survey.progress import EXECUTION_PROGRESS_STAGES, ExecutionProgressStage

logger = structlog.get_logger(__name__)

_RECOVERABLE_SURVEY_FINALIZATION_CODES = frozenset(
    {
        "survey_contract_violation",
        "survey_report_missing",
        "survey_outline_metadata_invalid",
        "survey_section_contract_invalid",
        "survey_reference_contract_invalid",
        "survey_finalization_output_invalid",
    }
)

SurveyStatus = Literal[
    "drafting", "queued", "running", "archiving", "succeeded", "failed", "cancelled"
]
QuotaState = Literal["reserved", "consumed", "released"]
JobStatus = Literal["queued", "running", "archiving", "finished"]
SurveyOutcome = Literal["succeeded", "failed", "cancelled"]


class SurveyQuotaExceededError(DBError):
    """The user has no free reservation in today's Survey allowance."""


class SurveyStateError(SurveyConflictError):
    """The requested aggregate transition is no longer valid."""

    def __init__(self, message: str, *, code: str = "survey_already_started") -> None:
        super().__init__(code, message)


@dataclass(frozen=True, slots=True)
class Survey:
    id: UUID
    user_id: int
    client_request_id: UUID
    request_hash: str | None
    initial_request: str
    title: str | None
    status: SurveyStatus
    quota_date: date
    quota_state: QuotaState
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    notify_on_completion: bool = False


@dataclass(frozen=True, slots=True)
class SurveyJob:
    id: UUID
    survey_id: UUID
    user_id: int
    approved_draft_id: UUID
    approved_draft: str
    approved_draft_revision: int
    client_request_id: UUID
    request_hash: str | None
    status: JobStatus
    terminal_outcome: SurveyOutcome | None
    storage_prefix: str | None
    storage_bucket: str | None
    manifest_key: str | None
    error_code: str | None
    error_message: str | None
    lease_owner: UUID | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    progress_stage: ExecutionProgressStage
    progress_updated_at: datetime
    archive_attempts: int
    next_archive_at: datetime | None
    queued_at: datetime
    last_claim_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SurveyProgressSnapshot:
    survey_id: UUID
    status: SurveyStatus
    execution_stage: ExecutionProgressStage | None
    queue_kind: Literal["draft", "survey"] | None
    queue_position: int | None
    queued_at: datetime | None
    running_slots: int
    started_at: datetime | None
    finished_at: datetime | None
    last_activity_at: datetime
    cancel_requested_at: datetime | None = None


def _survey(row: asyncpg.Record | dict[str, Any]) -> Survey:
    return Survey(
        id=row["id"],
        user_id=int(row["user_id"]),
        client_request_id=row["client_request_id"],
        request_hash=row["request_hash"],
        initial_request=str(row["initial_request"]),
        title=str(row["title"]) if row["title"] is not None else None,
        status=row["status"],
        quota_date=row["quota_date"],
        quota_state=row["quota_state"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        notify_on_completion=bool(row.get("notify_on_completion", False)),
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
        request_hash=row["request_hash"],
        status=row["status"],
        terminal_outcome=row["terminal_outcome"],
        storage_prefix=row["storage_prefix"],
        storage_bucket=row["storage_bucket"],
        manifest_key=row["manifest_key"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        heartbeat_at=row["heartbeat_at"],
        progress_stage=row["progress_stage"],
        progress_updated_at=row["progress_updated_at"],
        archive_attempts=int(row["archive_attempts"]),
        next_archive_at=row["next_archive_at"],
        queued_at=row["queued_at"],
        last_claim_at=row["last_claim_at"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        cancel_requested_at=row.get("cancel_requested_at"),
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
    request_hash: str,
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
                "SELECT usage.reserved_count, usage.succeeded_count, "
                "coalesce((SELECT daily_limit FROM scholight.user_quota_overrides "
                "WHERE user_id = $1 AND strength = 'survey'), $3::integer) AS daily_limit "
                "FROM scholight.survey_daily_usage AS usage "
                "WHERE usage.user_id = $1 AND usage.usage_date = $2 FOR UPDATE",
                user_id,
                quota_date,
                daily_limit,
            )
            existing = await connection.fetchrow(
                "SELECT * FROM scholight.surveys WHERE user_id = $1 AND client_request_id = $2",
                user_id,
                client_request_id,
            )
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise SurveyStateError(
                        "This idempotency key was already used for a different Survey request.",
                        code="survey_idempotency_conflict",
                    )
                return _survey(existing)
            if usage is None:
                raise DBError("Survey quota row was not created")
            effective_limit = int(usage.get("daily_limit", daily_limit))
            if int(usage["reserved_count"]) + int(usage["succeeded_count"]) >= effective_limit:
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
                "(id, user_id, client_request_id, request_hash, initial_request, quota_date) "
                "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
                survey_id,
                user_id,
                client_request_id,
                request_hash,
                initial_request,
                quota_date,
            )
            await connection.execute(
                "INSERT INTO scholight.survey_drafts "
                "(id, survey_id, client_request_id, request_hash, source, user_message) "
                "VALUES ($1, $2, $3, $4, 'generated', $5)",
                draft_id,
                survey_id,
                client_request_id,
                request_hash,
                initial_request,
            )
            if row is None:
                raise DBError("Survey was not created")
            return _survey(row)
    except (SurveyQuotaExceededError, SurveyStateError):
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


async def set_survey_title_if_missing(
    *, survey_id: UUID, user_id: int, title: str
) -> Survey | None:
    """Persist the first generated title without allowing later Drafts to rename the Survey."""
    try:
        row = await get_pool().fetchrow(
            "UPDATE scholight.surveys SET title = $3, updated_at = now() "
            "WHERE id = $1 AND user_id = $2 AND title IS NULL RETURNING *",
            survey_id,
            user_id,
            title,
        )
        if row is None:
            row = await get_pool().fetchrow(
                "SELECT * FROM scholight.surveys WHERE id = $1 AND user_id = $2",
                survey_id,
                user_id,
            )
        return _survey(row) if row is not None else None
    except asyncpg.PostgresError as exc:
        logger.error("survey_title_store_failed", error_type=type(exc).__name__)
        raise DBError("Failed to store Survey title") from exc


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


async def get_survey_progress(*, survey_id: UUID, user_id: int) -> SurveyProgressSnapshot | None:
    """Read one owner-scoped public progress snapshot without exposing workflow internals."""
    try:
        row = await get_pool().fetchrow(
            "WITH draft_ranked AS ("
            "SELECT d.id, row_number() OVER (ORDER BY user_turn, d.queued_at, d.id) AS position "
            "FROM scholight.survey_drafts d JOIN scholight.surveys ds ON ds.id = d.survey_id "
            "CROSS JOIN LATERAL (SELECT count(*) + 1 AS user_turn "
            "FROM scholight.survey_drafts p JOIN scholight.surveys ps ON ps.id = p.survey_id "
            "WHERE p.status = 'queued' AND ps.user_id = ds.user_id "
            "AND (p.queued_at, p.id) < (d.queued_at, d.id)) turn WHERE d.status = 'queued'), "
            "job_ranked AS ("
            "SELECT j.id, row_number() OVER (ORDER BY user_turn, j.queued_at, j.id) AS position "
            "FROM scholight.survey_jobs j JOIN scholight.surveys js ON js.id = j.survey_id "
            "CROSS JOIN LATERAL (SELECT count(*) + 1 AS user_turn "
            "FROM scholight.survey_jobs p JOIN scholight.surveys ps ON ps.id = p.survey_id "
            "WHERE p.status = 'queued' AND ps.user_id = js.user_id "
            "AND (p.queued_at, p.id) < (j.queued_at, j.id)) turn WHERE j.status = 'queued') "
            "SELECT s.id AS survey_id, s.status, s.started_at, s.finished_at, s.updated_at, "
            "j.progress_stage, j.progress_updated_at, j.heartbeat_at, "
            "j.cancel_requested_at, "
            "d.status AS draft_status, d.queued_at AS draft_queued_at, "
            "j.queued_at AS job_queued_at, dr.position AS draft_position, "
            "jr.position AS job_position, "
            "(SELECT count(*) FROM scholight.survey_drafts WHERE status = 'running' "
            "AND lease_expires_at > now()) AS running_drafts, "
            "(SELECT count(*) FROM scholight.survey_jobs WHERE status IN ('running','archiving') "
            "AND lease_owner IS NOT NULL AND lease_expires_at > now()) AS running_jobs "
            "FROM scholight.surveys s LEFT JOIN scholight.survey_jobs j ON j.survey_id = s.id "
            "LEFT JOIN scholight.survey_drafts d ON d.survey_id = s.id "
            "AND d.status IN ('queued','running') LEFT JOIN draft_ranked dr ON dr.id = d.id "
            "LEFT JOIN job_ranked jr ON jr.id = j.id "
            "WHERE s.id = $1 AND s.user_id = $2",
            survey_id,
            user_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_progress_read_failed", error_type=type(exc).__name__)
        raise DBError("Failed to read Survey progress") from exc
    if row is None:
        return None
    activity_candidates = [
        timestamp
        for timestamp in (row["updated_at"], row["progress_updated_at"], row["heartbeat_at"])
        if timestamp is not None
    ]
    draft_queue = row["status"] == "drafting" and row["draft_status"] is not None
    job_queue = row["status"] == "queued"
    return SurveyProgressSnapshot(
        survey_id=row["survey_id"],
        status=row["status"],
        execution_stage=row["progress_stage"],
        queue_kind="draft" if draft_queue else ("survey" if job_queue else None),
        queue_position=(
            int(row["draft_position"])
            if draft_queue and row["draft_position"] is not None
            else (
                int(row["job_position"]) if job_queue and row["job_position"] is not None else None
            )
        ),
        queued_at=row["draft_queued_at"] if draft_queue else row["job_queued_at"],
        running_slots=(int(row["running_drafts"]) if draft_queue else int(row["running_jobs"])),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        cancel_requested_at=row["cancel_requested_at"],
        last_activity_at=max(activity_candidates),
    )


async def start_survey(
    *,
    survey_id: UUID,
    user_id: int,
    job_id: UUID,
    client_request_id: UUID,
    request_hash: str,
    notify_on_completion: bool = False,
) -> Survey:
    """Bind the latest ready Draft and enqueue the only formal execution."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            locked = await lock_survey_aggregate(
                connection,
                survey_id=survey_id,
                user_id=user_id,
            )
            if locked is None:
                raise SurveyStateError("Survey not found", code="survey_not_found")
            row = locked.survey
            existing = locked.job
            if existing is not None:
                if existing["client_request_id"] == client_request_id:
                    if existing["request_hash"] != request_hash:
                        raise SurveyStateError(
                            "This idempotency key was already used to start another request.",
                            code="survey_idempotency_conflict",
                        )
                    return _survey(row)
                raise SurveyStateError(
                    "This Survey has already been started.", code="survey_already_started"
                )
            if row["status"] != "drafting" or row["quota_state"] != "reserved":
                raise SurveyStateError(
                    "This Survey has already been started.", code="survey_already_started"
                )
            active = any(draft["status"] in {"queued", "running"} for draft in locked.drafts)
            if active:
                raise SurveyStateError(
                    "Wait for the current Draft to finish before starting the Survey.",
                    code="survey_draft_in_progress",
                )
            draft = next(
                (
                    candidate
                    for candidate in sorted(
                        locked.drafts,
                        key=lambda item: item["revision"] or 0,
                        reverse=True,
                    )
                    if candidate["status"] == "ready"
                ),
                None,
            )
            if draft is None:
                raise SurveyStateError(
                    "Create a ready Draft before starting the Survey.",
                    code="survey_no_ready_draft",
                )
            await connection.execute(
                "INSERT INTO scholight.survey_jobs "
                "(id, survey_id, approved_draft_id, client_request_id, request_hash) "
                "VALUES ($1, $2, $3, $4, $5)",
                job_id,
                survey_id,
                draft["id"],
                client_request_id,
                request_hash,
            )
            updated = await connection.fetchrow(
                "UPDATE scholight.surveys SET status = 'queued', notify_on_completion = $2, "
                "updated_at = now() "
                "WHERE id = $1 RETURNING *",
                survey_id,
                notify_on_completion,
            )
            return _survey(updated)
    except SurveyStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_start_failed", error_type=type(exc).__name__)
        raise DBError("Failed to start Survey") from exc


async def cancel_survey(*, survey_id: UUID, user_id: int) -> Survey:
    """Cancel queued work immediately or request cooperative running cancellation."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            locked = await lock_survey_aggregate(
                connection,
                survey_id=survey_id,
                user_id=user_id,
            )
            if locked is None:
                raise SurveyStateError("Survey not found", code="survey_not_found")
            row = locked.survey
            if row["status"] in {"succeeded", "failed", "cancelled"}:
                return _survey(row)
            job = locked.job
            if row["status"] == "running":
                if job is None or job["status"] != "running":
                    raise SurveyStateError(
                        "This Survey cannot be cancelled in its current state.",
                        code="survey_not_cancellable",
                    )
                await connection.execute(
                    "UPDATE scholight.survey_jobs SET cancel_requested_at = coalesce("
                    "cancel_requested_at, now()), heartbeat_at = now() WHERE id = $1",
                    job["id"],
                )
                refreshed = await connection.fetchrow(
                    "UPDATE scholight.surveys SET updated_at = now() WHERE id = $1 RETURNING *",
                    survey_id,
                )
                return _survey(refreshed)
            if row["status"] == "archiving":
                raise SurveyStateError(
                    "This Survey cannot be cancelled in its current state.",
                    code="survey_not_cancellable",
                )
            if row["status"] not in {"drafting", "queued"}:
                raise SurveyStateError(
                    "This Survey cannot be cancelled in its current state.",
                    code="survey_not_cancellable",
                )
            if row["status"] == "queued":
                if job is None or job["status"] != "queued":
                    raise SurveyStateError(
                        "This Survey has already begun and can no longer be cancelled.",
                        code="survey_not_cancellable",
                    )
                await connection.execute(
                    "DELETE FROM scholight.survey_jobs WHERE id = $1", job["id"]
                )
            await connection.execute(
                "UPDATE scholight.survey_drafts SET status = 'cancelled', finished_at = now(), "
                "lease_owner = NULL, lease_expires_at = NULL "
                "WHERE survey_id = $1 AND status IN ('queued', 'running')",
                survey_id,
            )
            if int(locked.usage["reserved_count"]) <= 0:
                raise SurveyStateError(
                    "This Survey reservation is no longer available.",
                    code="survey_not_cancellable",
                )
            released = await connection.fetchrow(
                "UPDATE scholight.survey_daily_usage "
                "SET reserved_count = reserved_count - 1, updated_at = now() "
                "WHERE user_id = $1 AND usage_date = $2 AND reserved_count > 0 "
                "RETURNING reserved_count",
                user_id,
                row["quota_date"],
            )
            if released is None:
                raise SurveyStateError(
                    "This Survey reservation is no longer available.",
                    code="survey_not_cancellable",
                )
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


async def delete_survey(*, survey_id: UUID, user_id: int) -> None:
    """Delete one owner-scoped Survey after atomically releasing any queued reservation."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            locked = await lock_survey_aggregate(
                connection,
                survey_id=survey_id,
                user_id=user_id,
            )
            if locked is None:
                raise SurveyStateError("Survey not found", code="survey_not_found")
            survey = locked.survey
            if survey["status"] in {"running", "archiving"}:
                raise SurveyStateError(
                    "Cancel this Survey and wait for artifact archiving before deleting it.",
                    code="survey_delete_in_progress",
                )
            if survey["status"] in {"drafting", "queued"}:
                if locked.job is not None:
                    if locked.job["status"] != "queued":
                        raise SurveyStateError(
                            "Cancel this Survey before deleting it.",
                            code="survey_delete_in_progress",
                        )
                    await connection.execute(
                        "DELETE FROM scholight.survey_jobs WHERE id = $1", locked.job["id"]
                    )
                await connection.execute(
                    "UPDATE scholight.survey_drafts SET status = 'cancelled', "
                    "finished_at = now(), lease_owner = NULL, lease_expires_at = NULL "
                    "WHERE survey_id = $1 AND status IN ('queued','running')",
                    survey_id,
                )
                if survey["quota_state"] == "reserved":
                    released = await connection.fetchrow(
                        "UPDATE scholight.survey_daily_usage "
                        "SET reserved_count = reserved_count - 1, updated_at = now() "
                        "WHERE user_id = $1 AND usage_date = $2 AND reserved_count > 0 "
                        "RETURNING reserved_count",
                        user_id,
                        survey["quota_date"],
                    )
                    if released is None:
                        raise SurveyStateError(
                            "This Survey reservation is no longer available.",
                            code="survey_delete_in_progress",
                        )
            result = await connection.execute(
                "DELETE FROM scholight.surveys WHERE id = $1 AND user_id = $2",
                survey_id,
                user_id,
            )
            if str(result) != "DELETE 1":
                raise SurveyStateError("Survey not found", code="survey_not_found")
    except SurveyStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_delete_failed", error_type=type(exc).__name__)
        raise DBError("Failed to delete Survey") from exc


async def claim_survey_job(
    *,
    worker_id: UUID,
    lease_seconds: int,
    global_concurrency: int = 16,
    per_user_concurrency: int = 4,
) -> SurveyJob | None:
    """Atomically claim fairly within global and per-user capacity."""
    lease = timedelta(seconds=lease_seconds)
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            await lock_survey_capacity(connection, queue="job")
            running = await connection.fetchval(
                "SELECT count(*) FROM scholight.survey_jobs "
                "WHERE status IN ('running', 'archiving') AND lease_owner IS NOT NULL "
                "AND lease_expires_at > now()"
            )
            if int(running) >= global_concurrency:
                return None
            candidates = await connection.fetch(
                "WITH candidates AS ("
                "SELECT j.id, j.survey_id, j.status, j.queued_at, s.user_id, s.quota_date, "
                "row_number() OVER (PARTITION BY s.user_id ORDER BY "
                "CASE WHEN j.status = 'archiving' THEN 0 ELSE 1 END, j.queued_at, j.id) AS turn "
                "FROM scholight.survey_jobs j JOIN scholight.surveys s ON s.id = j.survey_id "
                "WHERE (j.status = 'queued' OR (j.status = 'archiving' "
                "AND (j.next_archive_at IS NULL OR j.next_archive_at <= now()) "
                "AND (j.lease_expires_at IS NULL OR j.lease_expires_at <= now())))), "
                "active AS (SELECT s.user_id, count(*) AS count "
                "FROM scholight.survey_jobs j JOIN scholight.surveys s ON s.id = j.survey_id "
                "WHERE j.status IN ('running', 'archiving') AND j.lease_owner IS NOT NULL "
                "AND j.lease_expires_at > now() GROUP BY s.user_id) "
                "SELECT c.* FROM candidates c LEFT JOIN active a ON a.user_id = c.user_id "
                "WHERE coalesce(a.count, 0) < $1 ORDER BY c.turn, c.queued_at, c.id LIMIT 32",
                per_user_concurrency,
            )
            for candidate in candidates:
                await connection.fetchrow(
                    "SELECT reserved_count FROM scholight.survey_daily_usage "
                    "WHERE user_id = $1 AND usage_date = $2 FOR UPDATE",
                    candidate["user_id"],
                    candidate["quota_date"],
                )
                survey = await connection.fetchrow(
                    "SELECT status FROM scholight.surveys WHERE id = $1 FOR UPDATE",
                    candidate["survey_id"],
                )
                job = await connection.fetchrow(
                    "SELECT id, status, approved_draft_id, lease_owner, lease_expires_at, "
                    "(lease_expires_at IS NULL OR lease_expires_at <= now()) AS claimable "
                    "FROM scholight.survey_jobs WHERE id = $1 FOR UPDATE SKIP LOCKED",
                    candidate["id"],
                )
                if job is None or survey is None:
                    continue
                active = await connection.fetchval(
                    "SELECT count(*) FROM scholight.survey_jobs j "
                    "JOIN scholight.surveys s ON s.id = j.survey_id "
                    "WHERE s.user_id = $1 AND j.id <> $2 "
                    "AND j.status IN ('running', 'archiving') AND j.lease_owner IS NOT NULL "
                    "AND j.lease_expires_at > now()",
                    candidate["user_id"],
                    candidate["id"],
                )
                if int(active) >= per_user_concurrency:
                    continue
                await connection.fetchrow(
                    "SELECT id FROM scholight.survey_drafts WHERE id = $1 FOR UPDATE",
                    job["approved_draft_id"],
                )
                if job["status"] == "queued":
                    if survey["status"] != "queued":
                        continue
                    await connection.execute(
                        "UPDATE scholight.surveys SET status = 'running', "
                        "started_at = coalesce(started_at, now()), updated_at = now() "
                        "WHERE id = $1",
                        candidate["survey_id"],
                    )
                    await connection.execute(
                        "UPDATE scholight.survey_jobs SET status = 'running', lease_owner = $2, "
                        "lease_expires_at = now() + $3, heartbeat_at = now(), "
                        "started_at = coalesce(started_at, now()), last_claim_at = now(), "
                        "progress_stage = 'planning', progress_updated_at = now() WHERE id = $1",
                        candidate["id"],
                        worker_id,
                        lease,
                    )
                elif job["status"] == "archiving" and bool(job["claimable"]):
                    await connection.execute(
                        "UPDATE scholight.survey_jobs SET lease_owner = $2, "
                        "lease_expires_at = now() + $3, heartbeat_at = now(), "
                        "last_claim_at = now() WHERE id = $1",
                        candidate["id"],
                        worker_id,
                        lease,
                    )
                else:
                    continue
                row = await connection.fetchrow(_JOB_SELECT + "WHERE j.id = $1", candidate["id"])
                return _job(row)
            return None
    except asyncpg.PostgresError as exc:
        logger.error("survey_job_claim_failed", error_type=type(exc).__name__)
        raise DBError("Failed to claim Survey job") from exc


async def heartbeat_survey_job(
    *, job_id: UUID, worker_id: UUID, lease_seconds: int
) -> HeartbeatState:
    try:
        row = await get_pool().fetchrow(
            "UPDATE scholight.survey_jobs SET heartbeat_at = now(), lease_expires_at = now() + $3 "
            "WHERE id = $1 AND lease_owner = $2 AND status IN ('running', 'archiving') "
            "RETURNING cancel_requested_at",
            job_id,
            worker_id,
            timedelta(seconds=lease_seconds),
        )
    except (asyncpg.PostgresError, DBError) as exc:
        logger.error("survey_job_heartbeat_failed", error_type=type(exc).__name__)
        return "transient_error"
    if row is None:
        return "lost"
    return "cancel_requested" if row["cancel_requested_at"] is not None else "owned"


async def update_survey_job_progress(
    *, job_id: UUID, worker_id: UUID, stage: ExecutionProgressStage
) -> bool:
    """Advance the durable execution milestone while the worker still owns the lease."""
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_jobs SET progress_stage = $3, progress_updated_at = now() "
            "WHERE id = $1 AND lease_owner = $2 AND status = 'running' "
            "AND cancel_requested_at IS NULL "
            "AND array_position($4::text[], progress_stage) "
            "<= array_position($4::text[], $3)",
            job_id,
            worker_id,
            stage,
            list(EXECUTION_PROGRESS_STAGES),
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_progress_update_failed", error_type=type(exc).__name__)
        raise DBError("Failed to update Survey progress") from exc
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
            locator = await connection.fetchrow(
                "SELECT survey_id FROM scholight.survey_jobs WHERE id = $1", job_id
            )
            if locator is None:
                raise SurveyStateError(
                    "The Survey execution no longer exists.", code="survey_worker_lost"
                )
            locked = await lock_survey_aggregate(connection, survey_id=locator["survey_id"])
            if locked is None or locked.job is None or locked.job["id"] != job_id:
                raise SurveyStateError(
                    "The Survey execution no longer exists.", code="survey_worker_lost"
                )
            row = await connection.fetchrow(_JOB_SELECT + "WHERE j.id = $1", job_id)
            if row is None:
                raise SurveyStateError(
                    "The Survey execution no longer exists.", code="survey_worker_lost"
                )
            effective_outcome: SurveyOutcome = (
                "cancelled" if row.get("cancel_requested_at") is not None else outcome
            )
            if row["status"] == "archiving" and row["terminal_outcome"] == effective_outcome:
                return _job(row)
            if row["status"] != "running" or row["lease_owner"] != worker_id:
                raise SurveyLeaseLostError("Survey execution lease is no longer owned")
            survey = locked.survey
            if (
                survey is None
                or survey["status"] != "running"
                or survey["quota_state"] != "reserved"
            ):
                raise SurveyLeaseLostError("Survey aggregate is no longer running")
            success_delta = 1 if effective_outcome == "succeeded" else 0
            usage = await connection.fetchrow(
                "UPDATE scholight.survey_daily_usage SET reserved_count = reserved_count - 1, "
                "succeeded_count = succeeded_count + $3, updated_at = now() "
                "WHERE user_id = $1 AND usage_date = $2 AND reserved_count > 0 RETURNING 1",
                survey["user_id"],
                survey["quota_date"],
                success_delta,
            )
            if usage is None:
                raise SurveyLeaseLostError("Survey reservation is no longer owned")
            await connection.execute(
                "UPDATE scholight.surveys SET status = 'archiving', quota_state = $2, "
                "error_code = $3, error_message = $4, updated_at = now() WHERE id = $1",
                row["survey_id"],
                "consumed" if effective_outcome == "succeeded" else "released",
                None if effective_outcome == "cancelled" else error_code,
                None if effective_outcome == "cancelled" else error_message,
            )
            await connection.execute(
                "UPDATE scholight.survey_jobs SET status = 'archiving', terminal_outcome = $2, "
                "error_code = $3, error_message = $4, next_archive_at = now(), heartbeat_at = now() "
                "WHERE id = $1",
                job_id,
                effective_outcome,
                None if effective_outcome == "cancelled" else error_code,
                None if effective_outcome == "cancelled" else error_message,
            )
            updated = await connection.fetchrow(_JOB_SELECT + "WHERE j.id = $1", job_id)
            return _job(updated)
    except (SurveyStateError, SurveyLeaseLostError):
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_execution_settle_failed", error_type=type(exc).__name__)
        raise DBError("Failed to settle Survey execution") from exc


async def finish_survey_archive(
    *,
    job_id: UUID,
    worker_id: UUID,
    storage_bucket: str,
    storage_prefix: str,
    manifest_key: str,
) -> SurveyJob:
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            locator = await connection.fetchrow(
                "SELECT survey_id FROM scholight.survey_jobs WHERE id = $1", job_id
            )
            if locator is None:
                raise SurveyLeaseLostError("Survey archive lease is no longer owned")
            locked = await lock_survey_aggregate(connection, survey_id=locator["survey_id"])
            if locked is None or locked.job is None or locked.job["id"] != job_id:
                raise SurveyLeaseLostError("Survey archive lease is no longer owned")
            row = await connection.fetchrow(
                _JOB_SELECT + "WHERE j.id = $1 AND j.lease_owner = $2 AND j.status = 'archiving'",
                job_id,
                worker_id,
            )
            if row is None:
                raise SurveyLeaseLostError("Survey archive lease is no longer owned")
            await connection.execute(
                "UPDATE scholight.survey_jobs SET status = 'finished', storage_bucket = $3, "
                "storage_prefix = $4, manifest_key = $5, lease_owner = NULL, "
                "lease_expires_at = NULL, "
                "heartbeat_at = now(), next_archive_at = NULL, finished_at = now() "
                "WHERE id = $1 AND lease_owner = $2",
                job_id,
                worker_id,
                storage_bucket,
                storage_prefix,
                manifest_key,
            )
            await connection.execute(
                "UPDATE scholight.surveys SET status = $2, updated_at = now(), finished_at = now() "
                "WHERE id = $1 AND status = 'archiving'",
                row["survey_id"],
                row["terminal_outcome"],
            )
            await connection.execute(
                "INSERT INTO scholight.survey_email_notifications "
                "(id, survey_id, user_id, survey_outcome) "
                "SELECT gen_random_uuid(), id, user_id, $2 FROM scholight.surveys "
                "WHERE id = $1 AND notify_on_completion "
                "AND $2::text IN ('succeeded', 'failed') "
                "ON CONFLICT (survey_id) DO NOTHING",
                row["survey_id"],
                row["terminal_outcome"],
            )
            updated = await connection.fetchrow(_JOB_SELECT + "WHERE j.id = $1", job_id)
            return _job(updated)
    except SurveyLeaseLostError:
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
        async with get_pool().acquire() as connection, connection.transaction():
            survey_id = await connection.fetchval(
                "SELECT survey_id FROM scholight.survey_jobs WHERE id = $1", job_id
            )
            if survey_id is None:
                raise SurveyLeaseLostError("Survey archive lease is no longer owned")
            locked = await lock_survey_aggregate(connection, survey_id=survey_id)
            if (
                locked is None
                or locked.job is None
                or locked.job["id"] != job_id
                or locked.job["lease_owner"] != worker_id
                or locked.job["status"] != "archiving"
            ):
                raise SurveyLeaseLostError("Survey archive lease is no longer owned")
            result = await connection.execute(
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
    except SurveyLeaseLostError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_archive_defer_failed", error_type=type(exc).__name__)
        raise DBError("Failed to defer Survey archive") from exc
    return str(result) == "UPDATE 1"


async def mark_survey_workspace_missing(*, job_id: UUID, worker_id: UUID) -> SurveyJob:
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            survey_id = await connection.fetchval(
                "SELECT survey_id FROM scholight.survey_jobs WHERE id = $1", job_id
            )
            if survey_id is None:
                raise SurveyLeaseLostError("Survey archive lease is no longer owned")
            locked = await lock_survey_aggregate(connection, survey_id=survey_id)
            if (
                locked is None
                or locked.job is None
                or locked.job["id"] != job_id
                or locked.job["lease_owner"] != worker_id
                or locked.job["status"] != "archiving"
            ):
                raise SurveyLeaseLostError("Survey archive lease is no longer owned")
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
                survey_id,
            )
            updated = await connection.fetchrow(_JOB_SELECT + "WHERE j.id = $1", job_id)
            return _job(updated)
    except SurveyLeaseLostError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_workspace_missing_update_failed", error_type=type(exc).__name__)
        raise DBError("Failed to record missing Survey workspace") from exc


async def recover_expired_survey_jobs(*, limit: int = 20) -> int:
    recovered = 0
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            rows = await connection.fetch(
                "SELECT id, survey_id FROM scholight.survey_jobs "
                "WHERE status = 'running' AND lease_expires_at <= now() "
                "ORDER BY lease_expires_at LIMIT $1",
                limit,
            )
            for row in rows:
                locked = await lock_survey_aggregate(connection, survey_id=row["survey_id"])
                if (
                    locked is None
                    or locked.job is None
                    or locked.job["id"] != row["id"]
                    or locked.job["status"] != "running"
                    or locked.job["lease_expires_at"] is None
                ):
                    continue
                expired = await connection.fetchval(
                    "SELECT lease_expires_at <= now() FROM scholight.survey_jobs WHERE id = $1",
                    row["id"],
                )
                if not expired:
                    continue
                survey = locked.survey
                usage = await connection.fetchrow(
                    "UPDATE scholight.survey_daily_usage SET reserved_count = reserved_count - 1, "
                    "updated_at = now() WHERE user_id = $1 AND usage_date = $2 "
                    "AND reserved_count > 0 RETURNING 1",
                    survey["user_id"],
                    survey["quota_date"],
                )
                if usage is None:
                    raise DBError("Expired Survey reservation is missing")
                cancelled = locked.job["cancel_requested_at"] is not None
                await connection.execute(
                    "UPDATE scholight.survey_jobs SET status = 'archiving', "
                    "terminal_outcome = $2, error_code = $3, error_message = $4, "
                    "lease_owner = NULL, lease_expires_at = NULL, next_archive_at = now(), "
                    "heartbeat_at = now() WHERE id = $1",
                    row["id"],
                    "cancelled" if cancelled else "failed",
                    None if cancelled else "survey_worker_lost",
                    None if cancelled else "The Survey worker stopped before completion.",
                )
                await connection.execute(
                    "UPDATE scholight.surveys SET status = 'archiving', quota_state = 'released', "
                    "error_code = $2, error_message = $3, "
                    "updated_at = now() WHERE id = $1",
                    row["survey_id"],
                    None if cancelled else "survey_worker_lost",
                    None if cancelled else "The Survey worker stopped before completion.",
                )
                recovered += 1
    except asyncpg.PostgresError as exc:
        logger.error("survey_expired_recovery_failed", error_type=type(exc).__name__)
        raise DBError("Failed to recover expired Survey jobs") from exc
    return recovered


async def recover_archived_survey_contract_failure(
    *,
    job_id: UUID,
    expected_manifest_key: str,
    expected_error_code: str,
    replacement_manifest_key: str | None,
) -> bool:
    """Atomically recover one verified archived finalization failure.

    Returns ``False`` when the exact recovery was already applied.  Artifact
    verification is deliberately performed before entering this short database
    transaction; the manifest key is rechecked here as the database-side guard.
    """
    if expected_error_code not in _RECOVERABLE_SURVEY_FINALIZATION_CODES:
        raise SurveyStateError(
            "The archived Survey error is not recoverable.",
            code="survey_recovery_ineligible",
        )
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            survey_id = await connection.fetchval(
                "SELECT survey_id FROM scholight.survey_jobs WHERE id = $1", job_id
            )
            if survey_id is None:
                raise SurveyStateError(
                    "The archived Survey execution does not exist.",
                    code="survey_recovery_not_found",
                )
            locked = await lock_survey_aggregate(connection, survey_id=survey_id)
            if locked is None or locked.job is None or locked.job["id"] != job_id:
                raise SurveyStateError(
                    "The archived Survey execution does not exist.",
                    code="survey_recovery_not_found",
                )
            survey = locked.survey
            job = locked.job
            expected_active_manifest = replacement_manifest_key or expected_manifest_key
            if (
                survey["status"] == "succeeded"
                and survey["quota_state"] == "consumed"
                and job["status"] == "finished"
                and job["terminal_outcome"] == "succeeded"
            ):
                if job["manifest_key"] != expected_active_manifest:
                    raise SurveyStateError(
                        "The recovered Survey manifest does not match.",
                        code="survey_recovery_manifest_changed",
                    )
                return False
            if job["manifest_key"] != expected_manifest_key:
                raise SurveyStateError(
                    "The archived Survey manifest changed during recovery.",
                    code="survey_recovery_manifest_changed",
                )
            storage_prefix = str(job["storage_prefix"] or "")
            expected_storage_prefix = f"surveys/v1/{survey['user_id']}/{job_id}"
            if (
                storage_prefix != expected_storage_prefix
                or expected_manifest_key != f"{storage_prefix}/manifest.json"
            ):
                raise SurveyStateError(
                    "The archived Survey ownership prefix is invalid.",
                    code="survey_recovery_manifest_changed",
                )
            if (
                replacement_manifest_key is not None
                and re.fullmatch(
                    rf"{re.escape(storage_prefix)}/recoveries/[0-9a-f]{{64}}/manifest\.json",
                    replacement_manifest_key,
                )
                is None
            ):
                raise SurveyStateError(
                    "The recovered Survey manifest ownership is invalid.",
                    code="survey_recovery_manifest_changed",
                )
            if not (
                survey["status"] == "failed"
                and survey["quota_state"] == "released"
                and survey["error_code"] == expected_error_code
                and job["status"] == "finished"
                and job["terminal_outcome"] == "failed"
                and job["error_code"] == expected_error_code
            ):
                raise SurveyStateError(
                    "The archived Survey is not an eligible finalization failure.",
                    code="survey_recovery_ineligible",
                )

            notification = await connection.fetchrow(
                "SELECT * FROM scholight.survey_email_notifications "
                "WHERE survey_id = $1 FOR UPDATE",
                survey_id,
            )
            if notification is not None and notification["status"] == "running":
                raise SurveyStateError(
                    "The Survey notification is currently being delivered.",
                    code="survey_recovery_notification_running",
                )

            usage = await connection.fetchrow(
                "UPDATE scholight.survey_daily_usage SET "
                "succeeded_count = succeeded_count + 1, updated_at = now() "
                "WHERE user_id = $1 AND usage_date = $2 RETURNING 1",
                survey["user_id"],
                survey["quota_date"],
            )
            if usage is None:
                raise SurveyStateError(
                    "The Survey quota ledger is unavailable.",
                    code="survey_recovery_quota_missing",
                )
            await connection.execute(
                "UPDATE scholight.surveys SET status = 'succeeded', quota_state = 'consumed', "
                "error_code = NULL, error_message = NULL, updated_at = now() WHERE id = $1",
                survey_id,
            )
            await connection.execute(
                "UPDATE scholight.survey_jobs SET terminal_outcome = 'succeeded', "
                "error_code = NULL, error_message = NULL, "
                "manifest_key = COALESCE($2, manifest_key) WHERE id = $1",
                job_id,
                replacement_manifest_key,
            )
            if notification is None:
                await connection.execute(
                    "INSERT INTO scholight.survey_email_notifications "
                    "(id, survey_id, user_id, survey_outcome) "
                    "SELECT gen_random_uuid(), id, user_id, 'succeeded' "
                    "FROM scholight.surveys WHERE id = $1 AND notify_on_completion "
                    "ON CONFLICT (survey_id) DO NOTHING",
                    survey_id,
                )
            else:
                await connection.execute(
                    "UPDATE scholight.survey_email_notifications SET survey_outcome = 'succeeded', "
                    "status = 'pending', attempts = 0, next_attempt_at = now(), lease_owner = NULL, "
                    "lease_expires_at = NULL, last_error = NULL, finished_at = NULL "
                    "WHERE survey_id = $1",
                    survey_id,
                )
            logger.info(
                "survey_archived_contract_failure_recovered",
                job_id=str(job_id),
                survey_id=str(survey_id),
                source_manifest_key=expected_manifest_key,
                manifest_key=expected_active_manifest,
            )
            return True
    except SurveyStateError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_archived_recovery_failed", error_type=type(exc).__name__)
        raise DBError("Failed to recover archived Survey") from exc


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
    "SurveyProgressSnapshot",
    "SurveyQuotaExceededError",
    "SurveyStateError",
    "cancel_survey",
    "claim_survey_job",
    "create_survey",
    "delete_survey",
    "defer_survey_archive",
    "finish_survey_archive",
    "get_survey",
    "get_survey_progress",
    "get_survey_job_counts",
    "heartbeat_survey_job",
    "list_surveys",
    "mark_survey_workspace_missing",
    "recover_expired_survey_jobs",
    "recover_archived_survey_contract_failure",
    "settle_survey_execution",
    "start_survey",
    "update_survey_job_progress",
]
