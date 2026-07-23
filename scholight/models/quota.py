"""Scholight-owned authenticated search quota models."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SearchStrengthValue = Literal["standard", "thorough"]
QuotaScope = Literal["anonymous_ip", "user"]
QuotaWindow = Literal["minute", "day"]


class QuotaStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    strength: SearchStrengthValue
    daily_limit: int = Field(ge=0)
    used: int = Field(ge=0)
    remaining: int = Field(ge=0)


class QuotaErrorDetails(BaseModel):
    """Machine-readable context explaining one exhausted search quota."""

    model_config = ConfigDict(frozen=True)

    scope: QuotaScope
    strength: SearchStrengthValue
    window: QuotaWindow
    limit: int = Field(ge=0)
    used: int = Field(ge=0)
    remaining: int = Field(ge=0)
    reset_at: datetime


class UserQuotaReservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: int
    strength: SearchStrengthValue
    quota_date: date
    used_count: int = Field(ge=1)
    daily_limit: int = Field(ge=1)


__all__ = [
    "QuotaErrorDetails",
    "QuotaScope",
    "QuotaStatus",
    "QuotaWindow",
    "SearchStrengthValue",
    "UserQuotaReservation",
]
