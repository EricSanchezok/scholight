"""Durable cleanup outbox for private Survey artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool
from scholight.survey.contracts import HeartbeatState, SurveyLeaseLostError

logger = structlog.get_logger(__name__)

CleanupStatus = Literal["pending", "running", "retry", "succeeded", "dead"]


@dataclass(frozen=True, slots=True)
class SurveyArtifactCleanup:
    id: UUID
    source_job_id: UUID
    user_id: int
    bucket: str
    storage_prefix: str
    manifest_key: str
    status: CleanupStatus
    attempts: int
    lease_owner: UUID | None
    lease_expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class SurveyArtifactCleanupStatus:
    pending: int
    running: int
    retry: int
    succeeded: int
    dead: int
    oldest_waiting_at: datetime | None


def _cleanup(row: asyncpg.Record | dict[str, Any]) -> SurveyArtifactCleanup:
    return SurveyArtifactCleanup(
        id=row["id"],
        source_job_id=row["source_job_id"],
        user_id=int(row["user_id_snapshot"]),
        bucket=str(row["bucket"]),
        storage_prefix=str(row["storage_prefix"]),
        manifest_key=str(row["manifest_key"]),
        status=row["status"],
        attempts=int(row["attempts"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
    )


async def claim_artifact_cleanup(
    *, worker_id: UUID, lease_seconds: int
) -> SurveyArtifactCleanup | None:
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT * FROM scholight.survey_artifact_cleanup_outbox "
                "WHERE status IN ('pending','retry') AND next_attempt_at <= now() "
                "ORDER BY next_attempt_at, created_at, id FOR UPDATE SKIP LOCKED LIMIT 1"
            )
            if row is None:
                return None
            updated = await connection.fetchrow(
                "UPDATE scholight.survey_artifact_cleanup_outbox SET status = 'running', "
                "lease_owner = $2, lease_expires_at = now() + $3, attempts = attempts + 1 "
                "WHERE id = $1 RETURNING *",
                row["id"],
                worker_id,
                timedelta(seconds=lease_seconds),
            )
            return _cleanup(updated)
    except asyncpg.PostgresError as exc:
        logger.error("survey_cleanup_claim_failed", error_type=type(exc).__name__)
        raise DBError("Failed to claim Survey artifact cleanup") from exc


async def heartbeat_artifact_cleanup(
    *, cleanup_id: UUID, worker_id: UUID, lease_seconds: int
) -> HeartbeatState:
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_artifact_cleanup_outbox SET lease_expires_at = now() + $3 "
            "WHERE id = $1 AND lease_owner = $2 AND status = 'running'",
            cleanup_id,
            worker_id,
            timedelta(seconds=lease_seconds),
        )
    except (asyncpg.PostgresError, DBError) as exc:
        logger.error("survey_cleanup_heartbeat_failed", error_type=type(exc).__name__)
        return "transient_error"
    return "owned" if str(result) == "UPDATE 1" else "lost"


async def complete_artifact_cleanup(*, cleanup_id: UUID, worker_id: UUID) -> None:
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_artifact_cleanup_outbox SET status = 'succeeded', "
            "lease_owner = NULL, lease_expires_at = NULL, last_error = NULL, "
            "finished_at = now() WHERE id = $1 AND lease_owner = $2 AND status = 'running'",
            cleanup_id,
            worker_id,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Failed to complete Survey artifact cleanup") from exc
    if str(result) != "UPDATE 1":
        raise SurveyLeaseLostError("Survey artifact cleanup lease is no longer owned")


async def retry_artifact_cleanup(
    *,
    cleanup_id: UUID,
    worker_id: UUID,
    delay: timedelta,
    error_message: str,
    dead: bool = False,
) -> None:
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_artifact_cleanup_outbox SET status = $3, "
            "next_attempt_at = now() + $4, lease_owner = NULL, lease_expires_at = NULL, "
            "last_error = $5, finished_at = CASE WHEN $3 = 'dead' THEN now() ELSE NULL END "
            "WHERE id = $1 AND lease_owner = $2 AND status = 'running'",
            cleanup_id,
            worker_id,
            "dead" if dead else "retry",
            delay,
            error_message[:1000],
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Failed to defer Survey artifact cleanup") from exc
    if str(result) != "UPDATE 1":
        raise SurveyLeaseLostError("Survey artifact cleanup lease is no longer owned")


async def recover_expired_artifact_cleanups() -> int:
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_artifact_cleanup_outbox SET status = 'retry', "
            "next_attempt_at = now(), lease_owner = NULL, lease_expires_at = NULL, "
            "last_error = 'Cleanup worker lease expired.' "
            "WHERE status = 'running' AND lease_expires_at <= now()"
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Failed to recover Survey artifact cleanups") from exc
    return int(str(result).rsplit(" ", 1)[-1])


async def get_artifact_cleanup_status() -> SurveyArtifactCleanupStatus:
    """Return low-cardinality cleanup health without exposing artifact locations."""
    try:
        row = await get_pool().fetchrow(
            "SELECT "
            "count(*) FILTER (WHERE status = 'pending')::int AS pending, "
            "count(*) FILTER (WHERE status = 'running')::int AS running, "
            "count(*) FILTER (WHERE status = 'retry')::int AS retry, "
            "count(*) FILTER (WHERE status = 'succeeded')::int AS succeeded, "
            "count(*) FILTER (WHERE status = 'dead')::int AS dead, "
            "min(created_at) FILTER (WHERE status IN ('pending','retry')) AS oldest_waiting_at "
            "FROM scholight.survey_artifact_cleanup_outbox"
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Failed to read Survey artifact cleanup status") from exc
    if row is None:
        raise DBError("Survey artifact cleanup status query returned no row")
    return SurveyArtifactCleanupStatus(
        pending=int(row["pending"]),
        running=int(row["running"]),
        retry=int(row["retry"]),
        succeeded=int(row["succeeded"]),
        dead=int(row["dead"]),
        oldest_waiting_at=row["oldest_waiting_at"],
    )


__all__ = [
    "SurveyArtifactCleanup",
    "SurveyArtifactCleanupStatus",
    "claim_artifact_cleanup",
    "complete_artifact_cleanup",
    "heartbeat_artifact_cleanup",
    "get_artifact_cleanup_status",
    "recover_expired_artifact_cleanups",
    "retry_artifact_cleanup",
]
