"""Aggregate Scholight administration metrics response models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AdminSyncState(BaseModel):
    last_successful_date: date | None
    last_started_at: datetime | None
    last_succeeded_at: datetime | None
    last_error_code: str | None
    last_error_message: str | None


class AdminQueueState(BaseModel):
    pending: int = Field(ge=0)
    running: int = Field(ge=0)
    retry: int = Field(ge=0)
    succeeded: int = Field(ge=0)
    dead: int = Field(ge=0)
    oldest_waiting_at: datetime | None


class AdminIntakePoint(BaseModel):
    day: date
    discovered: int = Field(ge=0)
    full_text_completed: int = Field(ge=0)


class AdminIngestionIssue(BaseModel):
    arxiv_id: str
    target_version: int = Field(ge=1)
    source: Literal["new", "revision", "reconciliation", "backfill", "manual"]
    status: Literal["retry", "dead"]
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    next_attempt_at: datetime
    last_error_code: str | None
    last_error_message: str | None
    updated_at: datetime


class AdminOperationsResponse(BaseModel):
    timezone: Literal["UTC"] = "UTC"
    generated_at: datetime
    sync: AdminSyncState | None
    queue: AdminQueueState
    intake: list[AdminIntakePoint]
    recent_issues: list[AdminIngestionIssue]


class AdminProfileMetrics(BaseModel):
    total: int = Field(ge=0)
    active: int = Field(ge=0)
    blocked: int = Field(ge=0)
    admins: int = Field(ge=0)
    created_in_period: int = Field(ge=0)


class AdminSearchMetrics(BaseModel):
    total: int = Field(ge=0)
    authenticated: int = Field(ge=0)
    anonymous: int = Field(ge=0)
    standard: int = Field(ge=0)
    thorough: int = Field(ge=0)
    authenticated_rest: int = Field(ge=0)
    authenticated_mcp: int = Field(ge=0)
    authenticated_success: int = Field(ge=0)
    authenticated_degraded: int = Field(ge=0)
    authenticated_failed: int = Field(ge=0)
    authenticated_p50_response_ms: float | None = Field(ge=0)
    authenticated_p95_response_ms: float | None = Field(ge=0)


class AdminAccessKeyMetrics(BaseModel):
    total: int = Field(ge=0)
    active: int = Field(ge=0)
    used_in_period: int = Field(ge=0)


class AdminDailyAnalyticsPoint(BaseModel):
    day: date
    total: int = Field(ge=0)
    authenticated: int = Field(ge=0)
    anonymous: int = Field(ge=0)
    standard: int = Field(ge=0)
    thorough: int = Field(ge=0)
    authenticated_rest: int = Field(ge=0)
    authenticated_mcp: int = Field(ge=0)


class AdminAnalyticsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    timezone: Literal["UTC"] = "UTC"
    from_: datetime = Field(alias="from")
    to: datetime
    profiles: AdminProfileMetrics
    searches: AdminSearchMetrics
    access_keys: AdminAccessKeyMetrics
    daily: list[AdminDailyAnalyticsPoint]


__all__ = [
    "AdminAnalyticsResponse",
    "AdminOperationsResponse",
]
