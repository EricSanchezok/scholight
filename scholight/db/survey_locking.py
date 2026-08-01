"""Canonical PostgreSQL lock order for Survey aggregate transitions."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import asyncpg


@dataclass(frozen=True, slots=True)
class LockedSurveyAggregate:
    survey: asyncpg.Record
    usage: asyncpg.Record
    job: asyncpg.Record | None
    drafts: tuple[asyncpg.Record, ...]


async def lock_survey_aggregate(
    connection: asyncpg.Connection,
    *,
    survey_id: UUID,
    user_id: int | None = None,
) -> LockedSurveyAggregate | None:
    """Lock usage, Survey, job, then Draft rows and revalidate ownership."""
    locator = await connection.fetchrow(
        "SELECT user_id, quota_date FROM scholight.surveys WHERE id = $1",
        survey_id,
    )
    if locator is None or (user_id is not None and int(locator["user_id"]) != user_id):
        return None
    usage = await connection.fetchrow(
        "SELECT * FROM scholight.survey_daily_usage "
        "WHERE user_id = $1 AND usage_date = $2 FOR UPDATE",
        locator["user_id"],
        locator["quota_date"],
    )
    survey = await connection.fetchrow(
        "SELECT * FROM scholight.surveys WHERE id = $1 FOR UPDATE",
        survey_id,
    )
    if survey is None or (user_id is not None and int(survey["user_id"]) != user_id):
        return None
    job = await connection.fetchrow(
        "SELECT * FROM scholight.survey_jobs WHERE survey_id = $1 FOR UPDATE",
        survey_id,
    )
    drafts = await connection.fetch(
        "SELECT * FROM scholight.survey_drafts WHERE survey_id = $1 ORDER BY id FOR UPDATE",
        survey_id,
    )
    if usage is None:
        raise RuntimeError("Survey quota row is missing")
    return LockedSurveyAggregate(
        survey=survey,
        usage=usage,
        job=job,
        drafts=tuple(drafts),
    )


__all__ = ["LockedSurveyAggregate", "lock_survey_aggregate"]
