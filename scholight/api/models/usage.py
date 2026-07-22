"""Public usage analytics response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class DailyQuotaUsage(BaseModel):
    used: int = Field(ge=0)
    daily_limit: int = Field(ge=0)
    remaining: int = Field(ge=0)


class TodayUsage(BaseModel):
    standard: DailyQuotaUsage
    thorough: DailyQuotaUsage


class UsageSummaryResponse(BaseModel):
    timezone: Literal["UTC"] = "UTC"
    reset_at: datetime
    today: TodayUsage
    searches_today: int = Field(ge=0)
    searches_this_month: int = Field(ge=0)
    typical_response_ms: float | None = Field(ge=0)
    p95_response_ms: float | None = Field(ge=0)
    success_rate: float | None = Field(ge=0, le=1)
    degraded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)


class VolumePoint(BaseModel):
    bucket_start: datetime
    standard: int = Field(ge=0)
    thorough: int = Field(ge=0)


class VolumeResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(alias="from")
    to: datetime
    bucket: Literal["day"] = "day"
    points: list[VolumePoint]


class LatencyPoint(BaseModel):
    bucket_start: datetime
    standard_p50_ms: float | None = Field(ge=0)
    thorough_p50_ms: float | None = Field(ge=0)
    overall_p95_ms: float | None = Field(ge=0)
    sample_count: int = Field(ge=0)


class LatencyResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: datetime = Field(alias="from")
    to: datetime
    bucket: Literal["day"] = "day"
    points: list[LatencyPoint]


class UsageAccessKey(BaseModel):
    id: UUID
    name: str
    last4: str


class UsageRecord(BaseModel):
    id: int
    created_at: datetime
    actor_type: Literal["web", "access_key"]
    access_key: UsageAccessKey | None
    strength: Literal["standard", "thorough"]
    search_duration_ms: float | None = Field(ge=0)
    result_count: int | None = Field(ge=0)
    outcome: Literal["success", "degraded", "failed"]
    quota_units: int = Field(ge=0)
    status_code: int | None
    error_code: str | None


class UsageRecordsResponse(BaseModel):
    items: list[UsageRecord]
    next_cursor: str | None


__all__ = [
    "DailyQuotaUsage",
    "LatencyPoint",
    "LatencyResponse",
    "TodayUsage",
    "UsageAccessKey",
    "UsageRecord",
    "UsageRecordsResponse",
    "UsageSummaryResponse",
    "VolumePoint",
    "VolumeResponse",
]
