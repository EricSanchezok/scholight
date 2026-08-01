"""Owner-scoped Survey list and artifact read projections."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool
from scholight.db.queries_survey import SurveyProgressSnapshot, SurveyStatus

logger = structlog.get_logger(__name__)

SurveyListView = Literal["active", "completed", "all"]


@dataclass(frozen=True, slots=True)
class SurveyQuotaSnapshot:
    daily_limit: int
    reserved: int
    succeeded: int

    @property
    def remaining(self) -> int:
        return max(0, self.daily_limit - self.reserved - self.succeeded)


@dataclass(frozen=True, slots=True)
class SurveySummary:
    id: UUID
    title: str | None
    initial_request: str
    status: SurveyStatus
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    latest_draft_revision: int | None
    progress: SurveyProgressSnapshot
    report_available: bool
    artifacts_available: bool


@dataclass(frozen=True, slots=True)
class SurveySummaryPage:
    items: tuple[SurveySummary, ...]
    quota: SurveyQuotaSnapshot
    has_more: bool


@dataclass(frozen=True, slots=True)
class SurveyArtifactReference:
    survey_id: UUID
    user_id: int
    job_id: UUID | None
    survey_status: SurveyStatus
    job_status: str | None
    terminal_outcome: str | None
    storage_bucket: str | None
    storage_prefix: str | None
    manifest_key: str | None


def _json_value(value: object) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _datetime(value: object) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise ValueError("Survey timestamp is invalid")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def list_survey_summaries(
    *,
    user_id: int,
    quota_date: date,
    daily_limit: int,
    view: SurveyListView,
    limit: int,
    cursor_created_at: datetime | None,
    cursor_id: UUID | None,
) -> SurveySummaryPage:
    """Fetch one page, live queue positions, and quota in a single SQL statement."""
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
            "AND (p.queued_at, p.id) < (j.queued_at, j.id)) turn WHERE j.status = 'queued'), "
            "selected AS (SELECT s.* FROM scholight.surveys s WHERE s.user_id = $1 "
            "AND ($2 = 'all' OR ($2 = 'active' AND s.status IN "
            "('drafting','queued','running','archiving')) OR ($2 = 'completed' AND s.status IN "
            "('succeeded','failed','cancelled'))) AND ($3::timestamptz IS NULL OR "
            "(s.created_at, s.id) < ($3, $4::uuid)) ORDER BY s.created_at DESC, s.id DESC "
            "LIMIT $5), item_rows AS (SELECT s.id, s.title, s.initial_request, s.status, "
            "s.created_at, "
            "s.updated_at, s.started_at, s.finished_at, j.progress_stage, "
            "j.progress_updated_at, j.heartbeat_at, j.cancel_requested_at, "
            "d.status AS draft_status, d.queued_at AS draft_queued_at, "
            "j.queued_at AS job_queued_at, dr.position AS draft_position, "
            "jr.position AS job_position, latest.revision AS latest_draft_revision, "
            "(SELECT count(*) FROM scholight.survey_drafts WHERE status = 'running' "
            "AND lease_expires_at > now()) AS running_drafts, "
            "(SELECT count(*) FROM scholight.survey_jobs WHERE status IN ('running','archiving') "
            "AND lease_owner IS NOT NULL AND lease_expires_at > now()) AS running_jobs, "
            "(s.status = 'succeeded' AND j.status = 'finished' "
            "AND j.manifest_key IS NOT NULL) AS report_available, "
            "(s.status IN ('succeeded','failed','cancelled') AND j.status = 'finished' "
            "AND j.manifest_key IS NOT NULL) AS artifacts_available "
            "FROM selected s LEFT JOIN scholight.survey_jobs j ON j.survey_id = s.id "
            "LEFT JOIN LATERAL (SELECT max(revision) AS revision FROM scholight.survey_drafts "
            "WHERE survey_id = s.id AND status = 'ready') latest ON true "
            "LEFT JOIN scholight.survey_drafts d ON d.survey_id = s.id "
            "AND d.status IN ('queued','running') LEFT JOIN draft_ranked dr ON dr.id = d.id "
            "LEFT JOIN job_ranked jr ON jr.id = j.id), quota AS (SELECT "
            "coalesce((SELECT reserved_count FROM scholight.survey_daily_usage "
            "WHERE user_id = $1 AND usage_date = $6), 0) AS reserved, "
            "coalesce((SELECT succeeded_count FROM scholight.survey_daily_usage "
            "WHERE user_id = $1 AND usage_date = $6), 0) AS succeeded) "
            "SELECT coalesce(jsonb_agg(to_jsonb(item_rows) ORDER BY created_at DESC, id DESC) "
            "FILTER (WHERE id IS NOT NULL), '[]'::jsonb) AS items, "
            "quota.reserved, quota.succeeded FROM quota LEFT JOIN item_rows ON true "
            "GROUP BY quota.reserved, quota.succeeded",
            user_id,
            view,
            cursor_created_at,
            cursor_id,
            limit + 1,
            quota_date,
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_summaries_read_failed", error_type=type(exc).__name__)
        raise DBError("Failed to list Survey summaries") from exc
    if row is None:
        raise DBError("Survey summary query returned no quota projection")
    raw_items = _json_value(row["items"])
    if not isinstance(raw_items, list):
        raise DBError("Survey summary projection is invalid")
    has_more = len(raw_items) > limit
    summaries: list[SurveySummary] = []
    for item in raw_items[:limit]:
        if not isinstance(item, dict):
            raise DBError("Survey summary projection is invalid")
        status = item["status"]
        draft_queue = status == "drafting" and item["draft_status"] is not None
        job_queue = status == "queued"
        created_at = _datetime(item["created_at"])
        updated_at = _datetime(item["updated_at"])
        if created_at is None or updated_at is None:
            raise DBError("Survey summary timestamp is missing")
        started_at = _datetime(item["started_at"])
        finished_at = _datetime(item["finished_at"])
        progress_updated_at = _datetime(item["progress_updated_at"])
        heartbeat_at = _datetime(item["heartbeat_at"])
        cancel_requested_at = _datetime(item["cancel_requested_at"])
        last_activity = max(
            value for value in (updated_at, progress_updated_at, heartbeat_at) if value is not None
        )
        snapshot = SurveyProgressSnapshot(
            survey_id=UUID(item["id"]),
            status=status,
            execution_stage=item["progress_stage"],
            queue_kind="draft" if draft_queue else ("survey" if job_queue else None),
            queue_position=(
                int(item["draft_position"])
                if draft_queue and item["draft_position"] is not None
                else (
                    int(item["job_position"])
                    if job_queue and item["job_position"] is not None
                    else None
                )
            ),
            queued_at=_datetime(item["draft_queued_at"] if draft_queue else item["job_queued_at"]),
            running_slots=(
                int(item["running_drafts"]) if draft_queue else int(item["running_jobs"])
            ),
            started_at=started_at,
            finished_at=finished_at,
            last_activity_at=last_activity,
            cancel_requested_at=cancel_requested_at,
        )
        summaries.append(
            SurveySummary(
                id=snapshot.survey_id,
                title=str(item["title"]) if item["title"] is not None else None,
                initial_request=str(item["initial_request"]),
                status=status,
                created_at=created_at,
                updated_at=updated_at,
                started_at=started_at,
                finished_at=finished_at,
                latest_draft_revision=(
                    int(item["latest_draft_revision"])
                    if item["latest_draft_revision"] is not None
                    else None
                ),
                progress=snapshot,
                report_available=bool(item["report_available"]),
                artifacts_available=bool(item["artifacts_available"]),
            )
        )
    return SurveySummaryPage(
        items=tuple(summaries),
        quota=SurveyQuotaSnapshot(
            daily_limit=daily_limit,
            reserved=int(row["reserved"]),
            succeeded=int(row["succeeded"]),
        ),
        has_more=has_more,
    )


async def get_survey_artifact_reference(
    *, survey_id: UUID, user_id: int
) -> SurveyArtifactReference | None:
    try:
        row = await get_pool().fetchrow(
            "SELECT s.id AS survey_id, s.user_id, j.id AS job_id, "
            "s.status AS survey_status, j.status AS job_status, "
            "j.terminal_outcome, j.storage_bucket, j.storage_prefix, j.manifest_key "
            "FROM scholight.surveys s LEFT JOIN scholight.survey_jobs j ON j.survey_id = s.id "
            "WHERE s.id = $1 AND s.user_id = $2",
            survey_id,
            user_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("survey_artifact_reference_read_failed", error_type=type(exc).__name__)
        raise DBError("Failed to read Survey artifact reference") from exc
    if row is None:
        return None
    return SurveyArtifactReference(
        survey_id=row["survey_id"],
        user_id=row["user_id"],
        job_id=row["job_id"],
        survey_status=row["survey_status"],
        job_status=row["job_status"],
        terminal_outcome=row["terminal_outcome"],
        storage_bucket=row["storage_bucket"],
        storage_prefix=row["storage_prefix"],
        manifest_key=row["manifest_key"],
    )


__all__ = [
    "SurveyArtifactReference",
    "SurveyListView",
    "SurveyQuotaSnapshot",
    "SurveySummary",
    "SurveySummaryPage",
    "get_survey_artifact_reference",
    "list_survey_summaries",
]
