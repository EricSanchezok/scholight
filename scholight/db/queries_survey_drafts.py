"""Immutable Survey Draft revisions and Draft worker leases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool
from scholight.db.queries_survey import SurveyStateError
from scholight.db.survey_locking import lock_survey_aggregate
from scholight.survey.contracts import HeartbeatState, SurveyLeaseLostError

logger = structlog.get_logger(__name__)

DraftSource = Literal["generated", "manual"]
DraftStatus = Literal["queued", "running", "ready", "failed", "cancelled"]


class SurveyDraftLimitError(SurveyStateError):
    """The Survey already contains ten successful Draft revisions."""

    def __init__(self, message: str = "Survey Draft revision limit reached") -> None:
        super().__init__(message, code="survey_draft_limit_reached")


@dataclass(frozen=True, slots=True)
class SurveyDraft:
    id: UUID
    survey_id: UUID
    user_id: int
    revision: int | None
    source: DraftSource
    user_message: str
    markdown: str | None
    status: DraftStatus
    based_on_revision: int | None
    client_request_id: UUID
    request_hash: str | None
    error_code: str | None
    error_message: str | None
    lease_owner: UUID | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    queued_at: datetime
    last_claim_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class SurveyDraftContext:
    initial_request: str
    history: tuple[tuple[str, str], ...]


def _draft(row: asyncpg.Record | dict[str, Any]) -> SurveyDraft:
    return SurveyDraft(
        id=row["id"],
        survey_id=row["survey_id"],
        user_id=int(row["user_id"]),
        revision=int(row["revision"]) if row["revision"] is not None else None,
        source=row["source"],
        user_message=str(row["user_message"]),
        markdown=str(row["markdown"]) if row["markdown"] is not None else None,
        status=row["status"],
        based_on_revision=(
            int(row["based_on_revision"]) if row["based_on_revision"] is not None else None
        ),
        client_request_id=row["client_request_id"],
        request_hash=row["request_hash"],
        error_code=row["error_code"],
        error_message=row["error_message"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        heartbeat_at=row["heartbeat_at"],
        queued_at=row["queued_at"],
        last_claim_at=row["last_claim_at"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


_DRAFT_SELECT = (
    "SELECT d.*, s.user_id FROM scholight.survey_drafts d "
    "JOIN scholight.surveys s ON s.id = d.survey_id "
)


async def list_survey_drafts(*, survey_id: UUID, user_id: int) -> list[SurveyDraft]:
    try:
        rows = await get_pool().fetch(
            _DRAFT_SELECT + "WHERE d.survey_id = $1 AND s.user_id = $2 ORDER BY d.created_at, d.id",
            survey_id,
            user_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_drafts_list_failed", error_type=type(exc).__name__)
        raise DBError("Failed to list Survey Drafts") from exc
    return [_draft(row) for row in rows]


async def request_generated_draft(
    *,
    survey_id: UUID,
    user_id: int,
    draft_id: UUID,
    client_request_id: UUID,
    request_hash: str,
    user_message: str,
) -> SurveyDraft:
    """Queue one model revision against the latest successful Draft."""
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
            existing = next(
                (
                    draft
                    for draft in locked.drafts
                    if draft["client_request_id"] == client_request_id
                ),
                None,
            )
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise SurveyStateError(
                        "This idempotency key was already used for a different Draft request.",
                        code="survey_idempotency_conflict",
                    )
                return _draft({**dict(existing), "user_id": user_id})
            if survey["status"] != "drafting":
                raise SurveyStateError(
                    "Survey Drafts can no longer be changed.", code="survey_already_started"
                )
            active = any(draft["status"] in {"queued", "running"} for draft in locked.drafts)
            if active:
                raise SurveyStateError(
                    "A Survey Draft is already being generated.",
                    code="survey_draft_in_progress",
                )
            revisions = [
                int(draft["revision"])
                for draft in locked.drafts
                if draft["status"] == "ready" and draft["revision"] is not None
            ]
            latest = max(revisions, default=None)
            if latest is not None and int(latest) >= 10:
                raise SurveyDraftLimitError("Survey Draft revision limit reached")
            await connection.execute(
                "INSERT INTO scholight.survey_drafts "
                "(id, survey_id, client_request_id, request_hash, source, user_message, "
                "based_on_revision) VALUES ($1, $2, $3, $4, 'generated', $5, $6)",
                draft_id,
                survey_id,
                client_request_id,
                request_hash,
                user_message,
                latest,
            )
            row = await connection.fetchrow(_DRAFT_SELECT + "WHERE d.id = $1", draft_id)
            return _draft(row)
    except (SurveyDraftLimitError, SurveyStateError):
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_draft_queue_failed", error_type=type(exc).__name__)
        raise DBError("Failed to queue Survey Draft") from exc


async def create_manual_draft(
    *,
    survey_id: UUID,
    user_id: int,
    draft_id: UUID,
    client_request_id: UUID,
    request_hash: str,
    user_message: str,
    markdown: str,
) -> SurveyDraft:
    """Append one ready immutable revision without invoking RCM."""
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
            existing = next(
                (
                    draft
                    for draft in locked.drafts
                    if draft["client_request_id"] == client_request_id
                ),
                None,
            )
            if existing is not None:
                if existing["request_hash"] != request_hash:
                    raise SurveyStateError(
                        "This idempotency key was already used for a different Draft request.",
                        code="survey_idempotency_conflict",
                    )
                return _draft({**dict(existing), "user_id": user_id})
            if survey["status"] != "drafting":
                raise SurveyStateError(
                    "Survey Drafts can no longer be changed.", code="survey_already_started"
                )
            active = any(draft["status"] in {"queued", "running"} for draft in locked.drafts)
            if active:
                raise SurveyStateError(
                    "A Survey Draft is already being generated.",
                    code="survey_draft_in_progress",
                )
            revisions = [
                int(draft["revision"])
                for draft in locked.drafts
                if draft["status"] == "ready" and draft["revision"] is not None
            ]
            latest = max(revisions, default=None)
            revision = 1 if latest is None else int(latest) + 1
            if revision > 10:
                raise SurveyDraftLimitError("Survey Draft revision limit reached")
            await connection.execute(
                "INSERT INTO scholight.survey_drafts "
                "(id, survey_id, client_request_id, request_hash, revision, source, "
                "user_message, markdown, "
                "status, based_on_revision, finished_at) "
                "VALUES ($1, $2, $3, $4, $5, 'manual', $6, $7, 'ready', $8, now())",
                draft_id,
                survey_id,
                client_request_id,
                request_hash,
                revision,
                user_message,
                markdown,
                latest,
            )
            row = await connection.fetchrow(_DRAFT_SELECT + "WHERE d.id = $1", draft_id)
            return _draft(row)
    except (SurveyDraftLimitError, SurveyStateError):
        raise
    except asyncpg.PostgresError as exc:
        logger.error("manual_survey_draft_create_failed", error_type=type(exc).__name__)
        raise DBError("Failed to create manual Survey Draft") from exc


async def claim_survey_draft(
    *, worker_id: UUID, lease_seconds: int, per_user_concurrency: int = 2
) -> SurveyDraft | None:
    """Claim queued Drafts in user-round-robin order."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            candidates = await connection.fetch(
                "WITH candidates AS ("
                "SELECT d.id, d.survey_id, d.queued_at, s.user_id, "
                "row_number() OVER (PARTITION BY s.user_id ORDER BY d.queued_at, d.id) AS turn "
                "FROM scholight.survey_drafts d JOIN scholight.surveys s ON s.id = d.survey_id "
                "WHERE d.status = 'queued'), active AS ("
                "SELECT s.user_id, count(*) AS count FROM scholight.survey_drafts d "
                "JOIN scholight.surveys s ON s.id = d.survey_id WHERE d.status = 'running' "
                "AND d.lease_owner IS NOT NULL AND d.lease_expires_at > now() GROUP BY s.user_id) "
                "SELECT c.* FROM candidates c LEFT JOIN active a ON a.user_id = c.user_id "
                "WHERE coalesce(a.count, 0) < $1 ORDER BY c.turn, c.queued_at, c.id LIMIT 32",
                per_user_concurrency,
            )
            for candidate in candidates:
                locked = await lock_survey_aggregate(connection, survey_id=candidate["survey_id"])
                if locked is None or locked.survey["status"] != "drafting":
                    continue
                draft = next(
                    (item for item in locked.drafts if item["id"] == candidate["id"]),
                    None,
                )
                if draft is None or draft["status"] != "queued":
                    continue
                active = await connection.fetchval(
                    "SELECT count(*) FROM scholight.survey_drafts d "
                    "JOIN scholight.surveys s ON s.id = d.survey_id "
                    "WHERE s.user_id = $1 AND d.status = 'running' "
                    "AND d.lease_owner IS NOT NULL AND d.lease_expires_at > now()",
                    candidate["user_id"],
                )
                if int(active) >= per_user_concurrency:
                    continue
                await connection.execute(
                    "UPDATE scholight.survey_drafts SET status = 'running', lease_owner = $2, "
                    "lease_expires_at = now() + $3, heartbeat_at = now(), "
                    "started_at = coalesce(started_at, now()), last_claim_at = now() "
                    "WHERE id = $1 AND status = 'queued'",
                    draft["id"],
                    worker_id,
                    timedelta(seconds=lease_seconds),
                )
                updated = await connection.fetchrow(_DRAFT_SELECT + "WHERE d.id = $1", draft["id"])
                return _draft(updated)
            return None
    except asyncpg.PostgresError as exc:
        logger.error("survey_draft_claim_failed", error_type=type(exc).__name__)
        raise DBError("Failed to claim Survey Draft") from exc


async def heartbeat_survey_draft(
    *, draft_id: UUID, worker_id: UUID, lease_seconds: int
) -> HeartbeatState:
    try:
        result = await get_pool().execute(
            "UPDATE scholight.survey_drafts SET heartbeat_at = now(), "
            "lease_expires_at = now() + $3 WHERE id = $1 AND lease_owner = $2 "
            "AND status = 'running'",
            draft_id,
            worker_id,
            timedelta(seconds=lease_seconds),
        )
    except (asyncpg.PostgresError, DBError) as exc:
        logger.error("survey_draft_heartbeat_failed", error_type=type(exc).__name__)
        return "transient_error"
    return "owned" if str(result) == "UPDATE 1" else "lost"


async def get_survey_draft_context(*, survey_id: UUID) -> SurveyDraftContext:
    try:
        survey = await get_pool().fetchrow(
            "SELECT initial_request FROM scholight.surveys WHERE id = $1", survey_id
        )
        rows = await get_pool().fetch(
            "SELECT user_message, markdown FROM scholight.survey_drafts "
            "WHERE survey_id = $1 AND status = 'ready' ORDER BY revision",
            survey_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_draft_context_read_failed", error_type=type(exc).__name__)
        raise DBError("Failed to read Survey Draft context") from exc
    if survey is None:
        raise SurveyStateError("Survey not found", code="survey_not_found")
    return SurveyDraftContext(
        initial_request=str(survey["initial_request"]),
        history=tuple((str(row["user_message"]), str(row["markdown"])) for row in rows),
    )


async def complete_survey_draft(*, draft_id: UUID, worker_id: UUID, markdown: str) -> SurveyDraft:
    """Assign the next revision only after RCM returned a valid Draft."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            survey_id = await connection.fetchval(
                "SELECT survey_id FROM scholight.survey_drafts WHERE id = $1", draft_id
            )
            if survey_id is None:
                raise SurveyLeaseLostError("Survey Draft lease is no longer owned")
            locked = await lock_survey_aggregate(connection, survey_id=survey_id)
            if locked is None:
                raise SurveyLeaseLostError("Survey Draft lease is no longer owned")
            row = next((item for item in locked.drafts if item["id"] == draft_id), None)
            if row is None or row["status"] != "running" or row["lease_owner"] != worker_id:
                raise SurveyLeaseLostError("Survey Draft lease is no longer owned")
            if locked.survey["status"] != "drafting":
                raise SurveyLeaseLostError("Survey Draft is no longer active")
            revisions = [
                int(draft["revision"])
                for draft in locked.drafts
                if draft["status"] == "ready" and draft["revision"] is not None
            ]
            latest = max(revisions, default=None)
            expected_base = int(latest) if latest is not None else None
            if row["based_on_revision"] != expected_base:
                raise SurveyLeaseLostError("Survey Draft was superseded by another revision")
            revision = 1 if latest is None else int(latest) + 1
            if revision > 10:
                raise SurveyDraftLimitError("Survey Draft revision limit reached")
            await connection.execute(
                "UPDATE scholight.survey_drafts SET status = 'ready', revision = $3, "
                "markdown = $4, lease_owner = NULL, lease_expires_at = NULL, "
                "heartbeat_at = now(), finished_at = now() WHERE id = $1 AND lease_owner = $2",
                draft_id,
                worker_id,
                revision,
                markdown,
            )
            updated = await connection.fetchrow(_DRAFT_SELECT + "WHERE d.id = $1", draft_id)
            return _draft(updated)
    except (SurveyDraftLimitError, SurveyStateError, SurveyLeaseLostError):
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_draft_complete_failed", error_type=type(exc).__name__)
        raise DBError("Failed to complete Survey Draft") from exc


async def fail_survey_draft(
    *, draft_id: UUID, worker_id: UUID, error_code: str, error_message: str
) -> SurveyDraft:
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            survey_id = await connection.fetchval(
                "SELECT survey_id FROM scholight.survey_drafts WHERE id = $1", draft_id
            )
            if survey_id is None:
                raise SurveyLeaseLostError("Survey Draft lease is no longer owned")
            locked = await lock_survey_aggregate(connection, survey_id=survey_id)
            if locked is None:
                raise SurveyLeaseLostError("Survey Draft lease is no longer owned")
            draft = next((item for item in locked.drafts if item["id"] == draft_id), None)
            if draft is None or draft["status"] != "running" or draft["lease_owner"] != worker_id:
                raise SurveyLeaseLostError("Survey Draft lease is no longer owned")
            row = await connection.fetchrow(
                "UPDATE scholight.survey_drafts SET status = 'failed', error_code = $3, "
                "error_message = $4, lease_owner = NULL, lease_expires_at = NULL, "
                "heartbeat_at = now(), finished_at = now() "
                "WHERE id = $1 AND lease_owner = $2 AND status = 'running' RETURNING *",
                draft_id,
                worker_id,
                error_code,
                error_message,
            )
            if row is None:
                raise SurveyLeaseLostError("Survey Draft lease is no longer owned")
            return _draft({**dict(row), "user_id": locked.survey["user_id"]})
    except SurveyLeaseLostError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("survey_draft_fail_failed", error_type=type(exc).__name__)
        raise DBError("Failed to record Survey Draft failure") from exc


async def recover_expired_survey_drafts(*, limit: int = 20) -> int:
    recovered = 0
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            candidates = await connection.fetch(
                "SELECT id, survey_id FROM scholight.survey_drafts "
                "WHERE status = 'running' AND lease_expires_at <= now() "
                "ORDER BY lease_expires_at LIMIT $1",
                limit,
            )
            for candidate in candidates:
                locked = await lock_survey_aggregate(connection, survey_id=candidate["survey_id"])
                if locked is None:
                    continue
                draft = next(
                    (item for item in locked.drafts if item["id"] == candidate["id"]),
                    None,
                )
                if draft is None or draft["status"] != "running":
                    continue
                expired = await connection.fetchval(
                    "SELECT lease_expires_at <= now() FROM scholight.survey_drafts WHERE id = $1",
                    candidate["id"],
                )
                if not expired:
                    continue
                await connection.execute(
                    "UPDATE scholight.survey_drafts SET status = 'failed', "
                    "error_code = 'survey_worker_lost', "
                    "error_message = 'Draft generation stopped before completion.', "
                    "lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = now(), "
                    "finished_at = now() WHERE id = $1",
                    candidate["id"],
                )
                recovered += 1
    except asyncpg.PostgresError as exc:
        logger.error("survey_draft_recovery_failed", error_type=type(exc).__name__)
        raise DBError("Failed to recover Survey Drafts") from exc
    return recovered


__all__ = [
    "SurveyDraft",
    "SurveyDraftContext",
    "SurveyDraftLimitError",
    "claim_survey_draft",
    "complete_survey_draft",
    "create_manual_draft",
    "fail_survey_draft",
    "get_survey_draft_context",
    "heartbeat_survey_draft",
    "list_survey_drafts",
    "recover_expired_survey_drafts",
    "request_generated_draft",
]
