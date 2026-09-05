"""Durable Survey compute attempts and exact, fenced task claims."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID, uuid4

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool
from scholight.db.queries_survey import _JOB_SELECT, SurveyJob, _job
from scholight.db.queries_survey_drafts import _DRAFT_SELECT, SurveyDraft, _draft
from scholight.db.survey_locking import lock_survey_aggregate, lock_survey_capacity
from scholight.survey.contracts import HeartbeatState

logger = structlog.get_logger(__name__)

WorkKind = Literal["draft", "full"]
AttemptStatus = Literal[
    "reserved",
    "launching",
    "launched",
    "running",
    "succeeded",
    "retryable",
    "failed",
    "cancelled",
]
ResourceProfile = Literal["draft", "full-standard", "full-high-memory"]

_ACTIVE_STATUSES = ("reserved", "launching", "launched", "running")
_FAILURE_DETAIL_KEYS = frozenset(
    {
        "container_reason",
        "provider_status",
        "provider_code",
        "provider_type",
        "provider_request_id",
        "request_bytes",
        "estimated_tokens",
        "message_count",
        "tool_count",
        "tool_result_count",
        "thinking_enabled",
        "reasoning_content_present",
        "reasoning_content_length",
        "unmatched_tool_calls",
        "duplicate_tool_calls",
        "checkpoint_error",
    }
)


@dataclass(frozen=True, slots=True)
class SurveyComputeAttempt:
    """One standalone ECS task reservation for one exact Survey work item."""

    id: UUID
    work_kind: WorkKind
    survey_id: UUID
    draft_id: UUID | None
    job_id: UUID | None
    attempt_no: int
    status: AttemptStatus
    resource_profile: ResourceProfile
    task_definition_arn: str
    client_token: str
    ecs_task_arn: str | None
    ecs_event_version: int | None
    current_stage: str | None
    current_unit: str | None
    checkpoint_sequence: int | None
    launch_failures: int
    next_launch_at: datetime
    exit_code: int | None
    stop_code: str | None
    stopped_reason: str | None
    failure_class: str | None
    failure_details: dict[str, object]
    peak_memory_bytes: int | None
    created_at: datetime
    launched_at: datetime | None
    started_at: datetime | None
    heartbeat_at: datetime | None
    stopped_at: datetime | None


@dataclass(frozen=True, slots=True)
class SurveyCheckpointPointer:
    sequence: int
    stage: str | None
    manifest_key: str | None
    manifest_sha256: str | None
    workflow_version: str
    executor_version: str
    execution_deadline_at: datetime


def _attempt(row: asyncpg.Record | dict[str, Any]) -> SurveyComputeAttempt:
    return SurveyComputeAttempt(
        id=row["id"],
        work_kind=row["work_kind"],
        survey_id=row["survey_id"],
        draft_id=row["draft_id"],
        job_id=row["job_id"],
        attempt_no=int(row["attempt_no"]),
        status=row["status"],
        resource_profile=row["resource_profile"],
        task_definition_arn=str(row["task_definition_arn"]),
        client_token=str(row["client_token"]),
        ecs_task_arn=(str(row["ecs_task_arn"]) if row["ecs_task_arn"] is not None else None),
        ecs_event_version=(
            int(row["ecs_event_version"]) if row["ecs_event_version"] is not None else None
        ),
        current_stage=row["current_stage"],
        current_unit=row["current_unit"],
        checkpoint_sequence=(
            int(row["checkpoint_sequence"]) if row["checkpoint_sequence"] is not None else None
        ),
        launch_failures=int(row["launch_failures"]),
        next_launch_at=row["next_launch_at"],
        exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,
        stop_code=row["stop_code"],
        stopped_reason=row["stopped_reason"],
        failure_class=row["failure_class"],
        failure_details=(
            json.loads(row["failure_details"])
            if isinstance(row["failure_details"], str)
            else dict(row["failure_details"])
        ),
        peak_memory_bytes=(
            int(row["peak_memory_bytes"]) if row["peak_memory_bytes"] is not None else None
        ),
        created_at=row["created_at"],
        launched_at=row["launched_at"],
        started_at=row["started_at"],
        heartbeat_at=row["heartbeat_at"],
        stopped_at=row["stopped_at"],
    )


async def reserve_next_compute_attempt(
    *,
    work_kind: WorkKind,
    task_definition_arn: str,
    global_concurrency: int,
    per_user_concurrency: int,
    max_compute_attempts: int = 3,
) -> SurveyComputeAttempt | None:
    """Fairly reserve one work item before calling ECS ``RunTask``."""
    if global_concurrency < 1 or per_user_concurrency < 1 or max_compute_attempts < 1:
        raise ValueError("Survey compute limits must be positive")
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            await lock_survey_capacity(connection, queue=work_kind)
            running = await connection.fetchval(
                "SELECT count(*) FROM scholight.survey_compute_attempts "
                "WHERE work_kind = $1 AND status = ANY($2::text[])",
                work_kind,
                list(_ACTIVE_STATUSES),
            )
            if int(running) >= global_concurrency:
                return None
            candidates = await _compute_candidates(
                connection,
                work_kind=work_kind,
                per_user_concurrency=per_user_concurrency,
                max_compute_attempts=max_compute_attempts,
            )
            for candidate in candidates:
                locked = await lock_survey_aggregate(
                    connection,
                    survey_id=candidate["survey_id"],
                )
                if locked is None:
                    continue
                target_id = candidate["target_id"]
                if not _candidate_is_current(locked, work_kind=work_kind, target_id=target_id):
                    continue
                active = await connection.fetchval(
                    "SELECT count(*) FROM scholight.survey_compute_attempts a "
                    "JOIN scholight.surveys s ON s.id = a.survey_id "
                    "WHERE a.work_kind = $1 AND s.user_id = $2 "
                    "AND a.status = ANY($3::text[])",
                    work_kind,
                    candidate["user_id"],
                    list(_ACTIVE_STATUSES),
                )
                if int(active) >= per_user_concurrency:
                    continue
                attempt_no = int(candidate["attempt_count"]) + 1
                attempt_id = uuid4()
                resource_profile: ResourceProfile
                if work_kind == "draft":
                    resource_profile = "draft"
                elif bool(candidate["had_oom"]):
                    resource_profile = "full-high-memory"
                else:
                    resource_profile = "full-standard"
                row = await connection.fetchrow(
                    "INSERT INTO scholight.survey_compute_attempts "
                    "(id, work_kind, survey_id, draft_id, job_id, attempt_no, "
                    "resource_profile, task_definition_arn, client_token) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING *",
                    attempt_id,
                    work_kind,
                    candidate["survey_id"],
                    target_id if work_kind == "draft" else None,
                    target_id if work_kind == "full" else None,
                    attempt_no,
                    resource_profile,
                    task_definition_arn,
                    str(attempt_id),
                )
                if row is None:
                    raise DBError("Survey compute attempt was not created")
                return _attempt(row)
            return None
    except asyncpg.PostgresError as exc:
        logger.error(
            "survey_compute_attempt_reserve_failed",
            work_kind=work_kind,
            error_type=type(exc).__name__,
        )
        raise DBError("Failed to reserve Survey compute attempt") from exc


async def _compute_candidates(
    connection: asyncpg.Connection,
    *,
    work_kind: WorkKind,
    per_user_concurrency: int,
    max_compute_attempts: int,
) -> list[asyncpg.Record]:
    if work_kind == "draft":
        draft_rows = await connection.fetch(
            "WITH eligible AS ("
            "SELECT d.id AS target_id, d.survey_id, d.queued_at, s.user_id, "
            "count(a.id)::integer AS attempt_count, false AS had_oom, "
            "row_number() OVER (PARTITION BY s.user_id ORDER BY d.queued_at, d.id) AS turn "
            "FROM scholight.survey_drafts d JOIN scholight.surveys s ON s.id = d.survey_id "
            "LEFT JOIN scholight.survey_compute_attempts a ON a.draft_id = d.id "
            "WHERE d.status = 'queued' AND s.status = 'drafting' "
            "AND NOT EXISTS (SELECT 1 FROM scholight.survey_compute_attempts active "
            "WHERE active.draft_id = d.id AND active.status = ANY($1::text[])) "
            "GROUP BY d.id, d.survey_id, d.queued_at, s.user_id), active_users AS ("
            "SELECT s.user_id, count(*)::integer AS count "
            "FROM scholight.survey_compute_attempts a "
            "JOIN scholight.surveys s ON s.id = a.survey_id "
            "WHERE a.work_kind = 'draft' AND a.status = ANY($1::text[]) GROUP BY s.user_id) "
            "SELECT e.* FROM eligible e LEFT JOIN active_users u ON u.user_id = e.user_id "
            "WHERE e.attempt_count < $2 AND coalesce(u.count, 0) < $3 "
            "ORDER BY e.turn, e.queued_at, e.target_id LIMIT 32",
            list(_ACTIVE_STATUSES),
            max_compute_attempts,
            per_user_concurrency,
        )
        return list(draft_rows)
    full_rows = await connection.fetch(
        "WITH eligible AS ("
        "SELECT j.id AS target_id, j.survey_id, j.queued_at, s.user_id, "
        "count(a.id)::integer AS attempt_count, "
        "coalesce(bool_or(a.failure_class = 'oom'), false) AS had_oom, "
        "row_number() OVER (PARTITION BY s.user_id ORDER BY "
        "CASE WHEN j.status = 'archiving' THEN 0 ELSE 1 END, j.queued_at, j.id) AS turn "
        "FROM scholight.survey_jobs j JOIN scholight.surveys s ON s.id = j.survey_id "
        "LEFT JOIN scholight.survey_compute_attempts a ON a.job_id = j.id "
        "WHERE j.status IN ('queued', 'running', 'archiving') "
        "AND j.terminal_outcome IS NULL "
        "AND (j.execution_deadline_at IS NULL OR j.execution_deadline_at > now()) "
        "AND (j.lease_expires_at IS NULL OR j.lease_expires_at <= now()) "
        "AND NOT EXISTS (SELECT 1 FROM scholight.survey_compute_attempts active "
        "WHERE active.job_id = j.id AND active.status = ANY($1::text[])) "
        "GROUP BY j.id, j.survey_id, j.queued_at, j.status, s.user_id), active_users AS ("
        "SELECT s.user_id, count(*)::integer AS count "
        "FROM scholight.survey_compute_attempts a "
        "JOIN scholight.surveys s ON s.id = a.survey_id "
        "WHERE a.work_kind = 'full' AND a.status = ANY($1::text[]) GROUP BY s.user_id) "
        "SELECT e.* FROM eligible e LEFT JOIN active_users u ON u.user_id = e.user_id "
        "WHERE e.attempt_count < $2 AND coalesce(u.count, 0) < $3 "
        "ORDER BY e.turn, e.queued_at, e.target_id LIMIT 32",
        list(_ACTIVE_STATUSES),
        max_compute_attempts,
        per_user_concurrency,
    )
    return list(full_rows)


def _candidate_is_current(locked: Any, *, work_kind: WorkKind, target_id: UUID) -> bool:
    if work_kind == "draft":
        return locked.survey["status"] == "drafting" and any(
            row["id"] == target_id and row["status"] == "queued" for row in locked.drafts
        )
    return (
        locked.job is not None
        and locked.job["id"] == target_id
        and locked.job["status"] in {"queued", "running", "archiving"}
        and locked.job["terminal_outcome"] is None
        and (
            locked.job["lease_expires_at"] is None
            or locked.job["lease_expires_at"] <= datetime.now(locked.job["queued_at"].tzinfo)
        )
    )


async def mark_compute_attempt_launching(*, attempt_id: UUID) -> SurveyComputeAttempt | None:
    """Move a due reservation to launching before the external ``RunTask`` call."""
    try:
        row = await get_pool().fetchrow(
            "UPDATE scholight.survey_compute_attempts SET status = 'launching' "
            "WHERE id = $1 AND status IN ('reserved', 'launching') "
            "AND next_launch_at <= now() RETURNING *",
            attempt_id,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Failed to mark Survey compute attempt launching") from exc
    return _attempt(row) if row is not None else None


async def mark_compute_attempt_launched(*, attempt_id: UUID, task_arn: str) -> SurveyComputeAttempt:
    """Persist the idempotent ECS task identity returned by ``RunTask``."""
    try:
        row = await get_pool().fetchrow(
            "UPDATE scholight.survey_compute_attempts "
            "SET status = 'launched', ecs_task_arn = $2, launched_at = coalesce(launched_at, now()) "
            "WHERE id = $1 AND status IN ('reserved', 'launching', 'launched') "
            "AND (ecs_task_arn IS NULL OR ecs_task_arn = $2) RETURNING *",
            attempt_id,
            task_arn,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Failed to record launched Survey compute attempt") from exc
    if row is None:
        raise DBError("Survey compute attempt is no longer launchable")
    return _attempt(row)


async def claim_exact_survey_draft(
    *, draft_id: UUID, attempt_id: UUID, lease_seconds: int
) -> SurveyDraft | None:
    """Claim only the Draft reserved for this one-shot task."""
    lease = timedelta(seconds=lease_seconds)
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            attempt = await connection.fetchrow(
                "SELECT * FROM scholight.survey_compute_attempts WHERE id = $1 FOR UPDATE",
                attempt_id,
            )
            if (
                attempt is None
                or attempt["work_kind"] != "draft"
                or attempt["draft_id"] != draft_id
                or attempt["status"] not in {"reserved", "launching", "launched"}
            ):
                return None
            locked = await lock_survey_aggregate(connection, survey_id=attempt["survey_id"])
            if locked is None or locked.survey["status"] != "drafting":
                return None
            draft = next((row for row in locked.drafts if row["id"] == draft_id), None)
            if draft is None or draft["status"] != "queued":
                return None
            await connection.execute(
                "UPDATE scholight.survey_drafts SET status = 'running', lease_owner = $2, "
                "lease_expires_at = now() + $3, heartbeat_at = now(), "
                "started_at = coalesce(started_at, now()), last_claim_at = now() "
                "WHERE id = $1 AND status = 'queued'",
                draft_id,
                attempt_id,
                lease,
            )
            await connection.execute(
                "UPDATE scholight.survey_compute_attempts SET status = 'running', "
                "started_at = coalesce(started_at, now()), heartbeat_at = now() WHERE id = $1",
                attempt_id,
            )
            row = await connection.fetchrow(_DRAFT_SELECT + "WHERE d.id = $1", draft_id)
            return _draft(row)
    except asyncpg.PostgresError as exc:
        logger.error("survey_exact_draft_claim_failed", error_type=type(exc).__name__)
        raise DBError("Failed to claim exact Survey Draft") from exc


async def claim_exact_survey_job(
    *,
    job_id: UUID,
    attempt_id: UUID,
    lease_seconds: int,
    workflow_version: str,
    executor_version: str,
    execution_timeout_seconds: int,
) -> SurveyJob | None:
    """Claim only the job and attempt embedded in this one-shot task command."""
    lease = timedelta(seconds=lease_seconds)
    deadline = timedelta(seconds=execution_timeout_seconds)
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            attempt = await connection.fetchrow(
                "SELECT * FROM scholight.survey_compute_attempts WHERE id = $1 FOR UPDATE",
                attempt_id,
            )
            if (
                attempt is None
                or attempt["work_kind"] != "full"
                or attempt["job_id"] != job_id
                or attempt["status"] not in {"reserved", "launching", "launched"}
            ):
                return None
            locked = await lock_survey_aggregate(connection, survey_id=attempt["survey_id"])
            if locked is None or locked.job is None or locked.job["id"] != job_id:
                return None
            job = locked.job
            if job["terminal_outcome"] is not None or job["status"] == "finished":
                return None
            now = datetime.now(job["queued_at"].tzinfo)
            if job["lease_expires_at"] is not None and job["lease_expires_at"] > now:
                return None
            if job["status"] == "queued":
                if locked.survey["status"] != "queued":
                    return None
                await connection.execute(
                    "UPDATE scholight.surveys SET status = 'running', "
                    "started_at = coalesce(started_at, now()), updated_at = now() WHERE id = $1",
                    job["survey_id"],
                )
            elif job["status"] not in {"running", "archiving"}:
                return None
            result = await connection.execute(
                "UPDATE scholight.survey_jobs SET "
                "status = CASE WHEN status = 'queued' THEN 'running' ELSE status END, "
                "lease_owner = $2, lease_expires_at = now() + $3, heartbeat_at = now(), "
                "started_at = coalesce(started_at, now()), last_claim_at = now(), "
                "progress_stage = CASE WHEN status = 'queued' THEN 'planning' ELSE progress_stage END, "
                "progress_updated_at = CASE WHEN status = 'queued' THEN now() "
                "ELSE progress_updated_at END, workflow_version = coalesce(workflow_version, $4), "
                "executor_version = coalesce(executor_version, $5), "
                "execution_deadline_at = coalesce(execution_deadline_at, now() + $6), "
                "resume_count = resume_count + CASE WHEN checkpoint_sequence IS NULL THEN 0 ELSE 1 END "
                "WHERE id = $1 AND status IN ('queued', 'running', 'archiving') "
                "AND terminal_outcome IS NULL RETURNING id",
                job_id,
                attempt_id,
                lease,
                workflow_version,
                executor_version,
                deadline,
            )
            if result != "UPDATE 1":
                return None
            await connection.execute(
                "UPDATE scholight.survey_compute_attempts SET status = 'running', "
                "started_at = coalesce(started_at, now()), heartbeat_at = now(), "
                "checkpoint_sequence = $2 WHERE id = $1",
                attempt_id,
                job["checkpoint_sequence"],
            )
            row = await connection.fetchrow(_JOB_SELECT + "WHERE j.id = $1", job_id)
            return _job(row)
    except asyncpg.PostgresError as exc:
        logger.error("survey_exact_job_claim_failed", error_type=type(exc).__name__)
        raise DBError("Failed to claim exact Survey job") from exc


async def commit_survey_job_checkpoint(
    *,
    job_id: UUID,
    attempt_id: UUID,
    expected_sequence: int,
    stage: str,
    manifest_key: str,
    manifest_sha256: str,
) -> bool:
    """CAS the durable checkpoint pointer while the attempt still owns the lease."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "UPDATE scholight.survey_jobs SET checkpoint_sequence = $3 + 1, "
                "checkpoint_stage = $4, checkpoint_manifest_key = $5, "
                "checkpoint_manifest_sha256 = $6, heartbeat_at = now() "
                "WHERE id = $1 AND lease_owner = $2 "
                "AND status IN ('running', 'archiving') "
                "AND coalesce(checkpoint_sequence, 0) = $3 RETURNING checkpoint_sequence",
                job_id,
                attempt_id,
                expected_sequence,
                stage,
                manifest_key,
                manifest_sha256,
            )
            if row is None:
                return False
            result = await connection.execute(
                "UPDATE scholight.survey_compute_attempts SET checkpoint_sequence = $2, "
                "current_stage = $3, heartbeat_at = now() "
                "WHERE id = $1 AND status = 'running'",
                attempt_id,
                int(row["checkpoint_sequence"]),
                stage,
            )
            if result != "UPDATE 1":
                raise DBError("Survey checkpoint attempt fencing failed")
            return True
    except asyncpg.PostgresError as exc:
        logger.error("survey_checkpoint_pointer_commit_failed", error_type=type(exc).__name__)
        raise DBError("Failed to commit Survey checkpoint pointer") from exc


async def get_claimed_job_checkpoint(*, job_id: UUID, attempt_id: UUID) -> SurveyCheckpointPointer:
    """Read the checkpoint pointer only while the exact attempt owns the job."""
    try:
        row = await get_pool().fetchrow(
            "SELECT checkpoint_sequence, checkpoint_stage, checkpoint_manifest_key, "
            "checkpoint_manifest_sha256, workflow_version, executor_version, "
            "execution_deadline_at FROM scholight.survey_jobs "
            "WHERE id = $1 AND lease_owner = $2 AND status IN ('running', 'archiving')",
            job_id,
            attempt_id,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Failed to read Survey checkpoint pointer") from exc
    if (
        row is None
        or row["workflow_version"] is None
        or row["executor_version"] is None
        or row["execution_deadline_at"] is None
    ):
        raise DBError("Survey checkpoint pointer is not owned or initialized")
    return SurveyCheckpointPointer(
        sequence=int(row["checkpoint_sequence"] or 0),
        stage=row["checkpoint_stage"],
        manifest_key=row["checkpoint_manifest_key"],
        manifest_sha256=row["checkpoint_manifest_sha256"],
        workflow_version=str(row["workflow_version"]),
        executor_version=str(row["executor_version"]),
        execution_deadline_at=row["execution_deadline_at"],
    )


async def heartbeat_compute_attempt(*, attempt_id: UUID, lease_seconds: int) -> HeartbeatState:
    """Renew an attempt and its exact work lease in one transaction."""
    lease = timedelta(seconds=lease_seconds)
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            attempt = await connection.fetchrow(
                "SELECT work_kind, draft_id, job_id FROM scholight.survey_compute_attempts "
                "WHERE id = $1 AND status = 'running' FOR UPDATE",
                attempt_id,
            )
            if attempt is None:
                return "lost"
            if attempt["work_kind"] == "draft":
                result = await connection.execute(
                    "UPDATE scholight.survey_drafts SET heartbeat_at = now(), "
                    "lease_expires_at = now() + $3 WHERE id = $1 AND lease_owner = $2 "
                    "AND status = 'running'",
                    attempt["draft_id"],
                    attempt_id,
                    lease,
                )
            else:
                row = await connection.fetchrow(
                    "UPDATE scholight.survey_jobs SET heartbeat_at = now(), "
                    "lease_expires_at = now() + $3 WHERE id = $1 AND lease_owner = $2 "
                    "AND status IN ('running', 'archiving') RETURNING cancel_requested_at",
                    attempt["job_id"],
                    attempt_id,
                    lease,
                )
                result = "UPDATE 1" if row is not None else "UPDATE 0"
            if result != "UPDATE 1":
                return "lost"
            await connection.execute(
                "UPDATE scholight.survey_compute_attempts SET heartbeat_at = now() WHERE id = $1",
                attempt_id,
            )
            if attempt["work_kind"] == "full" and row["cancel_requested_at"] is not None:
                return "cancel_requested"
            return "owned"
    except (asyncpg.PostgresError, DBError) as exc:
        logger.error("survey_compute_attempt_heartbeat_failed", error_type=type(exc).__name__)
        return "transient_error"


def _sanitized_failure_details(details: dict[str, object]) -> dict[str, object]:
    sanitized: dict[str, object] = {}
    for key in _FAILURE_DETAIL_KEYS:
        value = details.get(key)
        if isinstance(value, (bool, int, float)) or value is None:
            if key in details:
                sanitized[key] = value
        elif isinstance(value, str):
            sanitized[key] = value[:512]
    return sanitized


async def record_compute_attempt_stopped(
    *,
    attempt_id: UUID,
    event_version: int,
    exit_code: int | None,
    stop_code: str | None,
    stopped_reason: str | None,
    failure_class: str | None,
    failure_details: dict[str, object],
    retryable: bool,
) -> SurveyComputeAttempt:
    """Apply one ECS STOPPED event without allowing stale events to overwrite cause."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            current = await connection.fetchrow(
                "SELECT * FROM scholight.survey_compute_attempts WHERE id = $1 FOR UPDATE",
                attempt_id,
            )
            if current is None:
                raise DBError("Survey compute attempt was not found")
            current_version = current["ecs_event_version"]
            if current_version is not None and int(current_version) >= event_version:
                return _attempt(current)
            terminal = current["status"] in {"succeeded", "failed", "cancelled"}
            status = current["status"] if terminal else ("retryable" if retryable else "failed")
            row = await connection.fetchrow(
                "UPDATE scholight.survey_compute_attempts SET status = $2, "
                "ecs_event_version = $3, exit_code = $4, stop_code = $5, "
                "stopped_reason = $6, failure_class = $7, failure_details = $8::jsonb, "
                "stopped_at = coalesce(stopped_at, now()), heartbeat_at = now() "
                "WHERE id = $1 RETURNING *",
                attempt_id,
                status,
                event_version,
                exit_code,
                stop_code[:128] if stop_code is not None else None,
                stopped_reason[:1024] if stopped_reason is not None else None,
                failure_class[:64] if failure_class is not None else None,
                json.dumps(_sanitized_failure_details(failure_details)),
            )
            if row is None:
                raise DBError("Survey compute attempt stop was not recorded")
            if not terminal and current["work_kind"] == "full":
                await connection.execute(
                    "UPDATE scholight.survey_jobs SET lease_owner = NULL, "
                    "lease_expires_at = NULL, heartbeat_at = now() "
                    "WHERE id = $1 AND lease_owner = $2 "
                    "AND status IN ('running', 'archiving')",
                    current["job_id"],
                    attempt_id,
                )
            elif not terminal and current["work_kind"] == "draft":
                await connection.execute(
                    "UPDATE scholight.survey_drafts SET "
                    "status = CASE WHEN $3 THEN 'queued' ELSE 'failed' END, "
                    "lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = now(), "
                    "error_code = CASE WHEN $3 THEN NULL ELSE 'survey_draft_compute_failed' END, "
                    "error_message = CASE WHEN $3 THEN NULL "
                    "ELSE 'Draft compute task failed.' END "
                    "WHERE id = $1 AND lease_owner = $2 AND status = 'running'",
                    current["draft_id"],
                    attempt_id,
                    retryable,
                )
            return _attempt(row)
    except asyncpg.PostgresError as exc:
        logger.error("survey_compute_attempt_stop_failed", error_type=type(exc).__name__)
        raise DBError("Failed to record Survey compute attempt stop") from exc


__all__ = [
    "AttemptStatus",
    "ResourceProfile",
    "SurveyCheckpointPointer",
    "SurveyComputeAttempt",
    "WorkKind",
    "claim_exact_survey_draft",
    "claim_exact_survey_job",
    "commit_survey_job_checkpoint",
    "get_claimed_job_checkpoint",
    "heartbeat_compute_attempt",
    "mark_compute_attempt_launched",
    "mark_compute_attempt_launching",
    "record_compute_attempt_stopped",
    "reserve_next_compute_attempt",
]
