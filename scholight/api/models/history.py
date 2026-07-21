"""Public request and response models for search history."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from scholight.api.models.search import PublicSearchFilters, SearchStrength

StrictPositiveBigInt = Annotated[StrictInt, Field(ge=1, le=9_223_372_036_854_775_807)]


class PublicSearchHistoryItem(BaseModel):
    """One public history item without internal strategy or level fields."""

    model_config = ConfigDict(extra="forbid")

    id: int
    query: str
    strength: SearchStrength
    filters: PublicSearchFilters
    result_count: int = Field(ge=0)
    elapsed_ms: float = Field(ge=0, allow_inf_nan=False)
    created_at: datetime


class PublicSearchHistoryPage(BaseModel):
    """A paginated search-history response with an exact snapshot total."""

    model_config = ConfigDict(extra="forbid")

    items: list[PublicSearchHistoryItem]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)


class BulkDeleteSearchHistoryRequest(BaseModel):
    """Strict owner-scoped history IDs, stably deduplicated."""

    model_config = ConfigDict(extra="forbid")

    ids: list[StrictPositiveBigInt] = Field(min_length=1, max_length=100)

    @field_validator("ids")
    @staticmethod
    def _deduplicate_ids(ids: list[int]) -> list[int]:
        return list(dict.fromkeys(ids))


class BulkDeleteSearchHistoryResponse(BaseModel):
    """Number of active owner-scoped rows soft-deleted by a bulk request."""

    model_config = ConfigDict(extra="forbid")

    deleted: int = Field(ge=0)


__all__ = [
    "BulkDeleteSearchHistoryRequest",
    "BulkDeleteSearchHistoryResponse",
    "PublicSearchHistoryItem",
    "PublicSearchHistoryPage",
]
