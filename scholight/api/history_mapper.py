"""Mapping from internal history rows to the public HTTP contract."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import ValidationError

from scholight.api.models.history import PublicSearchHistoryItem, PublicSearchHistoryPage
from scholight.api.models.search import PublicSearchFilters, SearchStrength
from scholight.models.history import SearchHistoryEntry, SearchHistoryPage


def _map_list_filter(
    source: dict[str, object],
    field_name: Literal["categories", "authors"],
) -> list[str]:
    raw_values = source.get(field_name)
    if not isinstance(raw_values, list):
        return []

    normalized: list[str] = []
    for raw_value in raw_values:
        if not isinstance(raw_value, str):
            continue
        try:
            candidate = PublicSearchFilters.model_validate({field_name: [raw_value]})
        except ValidationError:
            continue
        value = candidate.categories[0] if field_name == "categories" else candidate.authors[0]
        if value not in normalized:
            normalized.append(value)
        if len(normalized) == 10:
            break
    return normalized


def _map_date_filter(
    source: dict[str, object],
    field_name: Literal["date_from", "date_to"],
) -> date | None:
    raw_value = source.get(field_name)
    if not isinstance(raw_value, str):
        return None
    try:
        candidate = PublicSearchFilters.model_validate({field_name: raw_value})
    except ValidationError:
        return None
    return candidate.date_from if field_name == "date_from" else candidate.date_to


def _map_filters(filters: dict[str, object] | None) -> PublicSearchFilters:
    source = filters or {}
    categories = _map_list_filter(source, "categories")
    authors = _map_list_filter(source, "authors")
    date_from = _map_date_filter(source, "date_from")
    date_to = _map_date_filter(source, "date_to")
    if date_from is not None and date_to is not None and date_from > date_to:
        date_from = None
        date_to = None
    return PublicSearchFilters(
        categories=categories,
        authors=authors,
        date_from=date_from,
        date_to=date_to,
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
