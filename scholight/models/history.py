"""Search-history data models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SearchHistoryRecord(BaseModel):
    """Full search-history row from the ``search_history`` table (internal use)."""

    id: int
    user_id: int
    query_text: str
    strength: Literal["standard", "thorough"]
    filters: dict[str, object] | None = None
    result_count: int
    response_time_ms: float | None = None
    created_at: datetime | None = None
    deleted_at: datetime | None = None


class SearchHistoryEntry(BaseModel):
    """User-facing search-history item returned by the API."""

    id: int
    query_text: str
    strength: Literal["standard", "thorough"]
    filters: dict[str, object] | None = None
    result_count: int
    response_time_ms: float | None = None
    created_at: datetime | None = None


class SearchHistoryPage(BaseModel):
    """One consistent history snapshot returned by the query layer."""

    items: list[SearchHistoryEntry]
    total: int


__all__ = [
    "SearchHistoryEntry",
    "SearchHistoryPage",
    "SearchHistoryRecord",
]
