"""Scholight-owned authenticated search quota models."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SearchStrengthValue = Literal["standard", "thorough"]


class QuotaStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    strength: SearchStrengthValue
    daily_limit: int = Field(ge=0)
    used: int = Field(ge=0)
    remaining: int = Field(ge=0)


class UserQuotaReservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: int
    strength: SearchStrengthValue
    quota_date: date
    used_count: int = Field(ge=1)
    daily_limit: int = Field(ge=1)


__all__ = ["QuotaStatus", "SearchStrengthValue", "UserQuotaReservation"]
