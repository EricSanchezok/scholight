"""Durable outbox queries for Survey completion email notifications."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool
from scholight.survey.contracts import SurveyLeaseLostError

logger = structlog.get_logger(__name__)

NotificationStatus = Literal["pending", "running", "retry", "succeeded", "dead"]
NotificationOutcome = Literal["succeeded", "failed"]


@dataclass(frozen=True, slots=True)
class SurveyEmailNotification:
    id: UUID
    survey_id: UUID
    user_id: int
    survey_outcome: NotificationOutcome
    recipient_email: str
    recipient_verified: bool
    survey_title: str
    started_at: datetime | None
    finished_at: datetime
    survey_error_code: str | None
    status: NotificationStatus
    attempts: int
    lease_owner: UUID | None
    lease_expires_at: datetime | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class SurveyEmailNotificationStatus:
    pending: int
    running: int
    retry: int
    succeeded: int
    dead: int
    oldest_waiting_at: datetime | None


def _notification(row: asyncpg.Record | dict[str, Any]) -> SurveyEmailNotification:
    finished_at = row["survey_finished_at"]
    if finished_at is None:
        raise DBError("Survey email notification has no terminal timestamp")
    return SurveyEmailNotification(
        id=row["id"],
        survey_id=row["survey_id"],
        user_id=int(row["user_id"]),
        survey_outcome=row["survey_outcome"],
        recipient_email=str(row["recipient_email"]),
        recipient_verified=bool(row["recipient_verified"]),
        survey_title=str(row["survey_title"]),
        started_at=row["survey_started_at"],
        finished_at=finished_at,
        survey_error_code=(
            str(row["survey_error_code"]) if row.get("survey_error_code") is not None else None
        ),
        status=row["status"],
        attempts=int(row["attempts"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        created_at=row["created_at"],
    )


async def claim_email_notification(
    *, worker_id: UUID, lease_seconds: int
) -> SurveyEmailNotification | None:
    """Claim one eligible notification with its current verified account destination."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = await connection.fetchrow(
                "SELECT notifications.*, users.email AS recipient_email, "
                "users.email_verified_at IS NOT NULL AS recipient_verified, "
                "coalesce(nullif(btrim(surveys.title), ''), 'Your research survey') "
                "AS survey_title, surveys.started_at AS survey_started_at, "
                "surveys.finished_at AS survey_finished_at, "
                "surveys.error_code AS survey_error_code "
                "FROM scholight.survey_email_notifications AS notifications "
                "JOIN scholight.surveys AS surveys ON surveys.id = notifications.survey_id "
                "JOIN auth.users AS users ON users.id = notifications.user_id "
                "WHERE notifications.status IN ('pending','retry') "
                "AND notifications.next_attempt_at <= now() "
                "ORDER BY notifications.next_attempt_at, notifications.created_at, "
                "notifications.id FOR UPDATE OF notifications SKIP LOCKED LIMIT 1"
            )
            if row is None:
                return None
            updated = await connection.fetchrow(
                "UPDATE scholight.survey_email_notifications SET status = 'running', "
                "lease_owner = $2, lease_expires_at = now() + $3, attempts = attempts + 1 "
                "WHERE id = $1 RETURNING *",
                row["id"],
                worker_id,
                timedelta(seconds=lease_seconds),
            )
            if updated is None:
                raise DBError("Survey email notification was not claimed")
            return _notification({**dict(row), **dict(updated)})
    except asyncpg.PostgresError as exc:
        logger.error("survey_email_notification_claim_failed", error_type=type(exc).__name__)
        raise DBError("Failed to claim Survey email notification") from exc


async def complete_email_notification(*, notification_id: UUID, worker_id: UUID) -> None:
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_email_notifications SET status = 'succeeded', "
            "lease_owner = NULL, lease_expires_at = NULL, last_error = NULL, "
            "finished_at = now() WHERE id = $1 AND lease_owner = $2 AND status = 'running'",
            notification_id,
            worker_id,
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Failed to complete Survey email notification") from exc
    if str(result) != "UPDATE 1":
        raise SurveyLeaseLostError("Survey email notification lease is no longer owned")


async def retry_email_notification(
    *,
    notification_id: UUID,
    worker_id: UUID,
    delay: timedelta,
    error_code: str,
    dead: bool,
) -> None:
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_email_notifications SET status = $3, "
            "next_attempt_at = now() + $4, lease_owner = NULL, lease_expires_at = NULL, "
            "last_error = $5, finished_at = CASE WHEN $3 = 'dead' THEN now() ELSE NULL END "
            "WHERE id = $1 AND lease_owner = $2 AND status = 'running'",
            notification_id,
            worker_id,
            "dead" if dead else "retry",
            delay,
            error_code[:128],
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Failed to defer Survey email notification") from exc
    if str(result) != "UPDATE 1":
        raise SurveyLeaseLostError("Survey email notification lease is no longer owned")


async def recover_expired_email_notifications() -> int:
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_email_notifications SET status = 'retry', "
            "next_attempt_at = now(), lease_owner = NULL, lease_expires_at = NULL, "
            "last_error = 'worker_lease_expired' "
            "WHERE status = 'running' AND lease_expires_at <= now()"
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Failed to recover Survey email notifications") from exc
    return int(str(result).rsplit(" ", 1)[-1])


async def get_email_notification_status() -> SurveyEmailNotificationStatus:
    try:
        row = await get_pool().fetchrow(
            "SELECT count(*) FILTER (WHERE status = 'pending')::int AS pending, "
            "count(*) FILTER (WHERE status = 'running')::int AS running, "
            "count(*) FILTER (WHERE status = 'retry')::int AS retry, "
            "count(*) FILTER (WHERE status = 'succeeded')::int AS succeeded, "
            "count(*) FILTER (WHERE status = 'dead')::int AS dead, "
            "min(created_at) FILTER (WHERE status IN ('pending','retry')) AS oldest_waiting_at "
            "FROM scholight.survey_email_notifications"
        )
    except asyncpg.PostgresError as exc:
        raise DBError("Failed to read Survey email notification status") from exc
    if row is None:
        raise DBError("Survey email notification status query returned no row")
    return SurveyEmailNotificationStatus(
        pending=int(row["pending"]),
        running=int(row["running"]),
        retry=int(row["retry"]),
        succeeded=int(row["succeeded"]),
        dead=int(row["dead"]),
        oldest_waiting_at=row["oldest_waiting_at"],
    )


__all__ = [
    "SurveyEmailNotification",
    "SurveyEmailNotificationStatus",
    "claim_email_notification",
    "complete_email_notification",
    "get_email_notification_status",
    "recover_expired_email_notifications",
    "retry_email_notification",
]
