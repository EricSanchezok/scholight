"""Mapping from internal history rows to the public HTTP contract."""

from __future__ import annotations

from scholight.api.models.history import PublicSearchHistoryItem, PublicSearchHistoryPage
from scholight.api.models.search import PublicSearchFilters, SearchStrength
from scholight.models.history import SearchHistoryEntry, SearchHistoryPage


def _map_filters(filters: dict[str, object] | None) -> PublicSearchFilters:
    source = filters or {}
    return PublicSearchFilters.model_validate(
        {
            "categories": source.get("categories", []),
            "authors": source.get("authors", []),
            "date_from": source.get("date_from"),
            "date_to": source.get("date_to"),
        }
    )


def _map_item(entry: SearchHistoryEntry) -> PublicSearchHistoryItem:
    if entry.level == 1:
        strength = SearchStrength.STANDARD
    elif entry.level == 2:
        strength = SearchStrength.THOROUGH
    else:
        raise ValueError("legacy history levels must be excluded by the query layer")
    if entry.response_time_ms is None or entry.created_at is None:
        raise ValueError("history timing fields must be present")
    return PublicSearchHistoryItem(
        id=entry.id,
        query=entry.query_text,
        strength=strength,
        filters=_map_filters(entry.filters),
        result_count=entry.num_results,
        elapsed_ms=entry.response_time_ms,
        created_at=entry.created_at,
    )


def map_search_history_page(
    page: SearchHistoryPage,
    *,
    limit: int,
    offset: int,
) -> PublicSearchHistoryPage:
    """Map one consistent internal snapshot without exposing legacy diagnostics."""
    return PublicSearchHistoryPage(
        items=[_map_item(entry) for entry in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
    )


__all__ = ["map_search_history_page"]
