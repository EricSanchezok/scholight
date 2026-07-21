"""Parameterized PostgreSQL queries for search history."""

from __future__ import annotations

import json

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool
from scholight.models.history import SearchHistoryEntry, SearchHistoryPage

logger = structlog.get_logger(__name__)

_COUNT_HISTORY_SQL = """
SELECT
    count(*) FILTER (WHERE level IN (1, 2)) AS total,
    count(*) FILTER (WHERE level = 3) AS legacy_level3_count
FROM public.search_history
WHERE user_id = $1
  AND deleted_at IS NULL
"""
_COUNT_FILTERED_HISTORY_SQL = _COUNT_HISTORY_SQL + "  AND query_text ILIKE $2 ESCAPE '\\'\n"
_PAGE_HISTORY_SQL = """
SELECT id, query_text, level, strategy, filters,
       num_results, response_time_ms, created_at
FROM public.search_history
WHERE user_id = $1
  AND deleted_at IS NULL
  AND level IN (1, 2)
ORDER BY created_at DESC, id DESC
LIMIT $2 OFFSET $3
"""
_PAGE_FILTERED_HISTORY_SQL = """
SELECT id, query_text, level, strategy, filters,
       num_results, response_time_ms, created_at
FROM public.search_history
WHERE user_id = $1
  AND deleted_at IS NULL
  AND query_text ILIKE $2 ESCAPE '\\'
  AND level IN (1, 2)
ORDER BY created_at DESC, id DESC
LIMIT $3 OFFSET $4
"""
_BULK_DELETE_HISTORY_SQL = """
UPDATE public.search_history
SET deleted_at = statement_timestamp()
WHERE user_id = $1
  AND deleted_at IS NULL
  AND id = ANY($2::bigint[])
RETURNING id
"""


def _literal_ilike_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _history_entry(row: asyncpg.Record | dict[str, object]) -> SearchHistoryEntry:
    raw: dict[str, object] = dict(row)
    filters = raw.get("filters")
    if isinstance(filters, str):
        raw["filters"] = json.loads(filters)
    return SearchHistoryEntry.model_validate(raw)


async def log_search(
    user_id: int,
    query_text: str,
    level: int,
    strategy: str | None,
    filters: dict[str, object] | None,
    num_results: int,
    response_time_ms: float,
) -> int:
    """Insert a search-history row and return its id."""
    filters_json = json.dumps(filters) if filters is not None else None
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            search_id: int = await conn.fetchval(
                "INSERT INTO public.search_history "
                "(user_id, query_text, level, strategy, filters, num_results, response_time_ms) "
                "VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7) RETURNING id",
                user_id,
                query_text,
                level,
                strategy,
                filters_json,
                num_results,
                response_time_ms,
            )
    except asyncpg.PostgresError as exc:
        logger.error("log_search_failed", user_id=user_id, error_type=type(exc).__name__)
        raise DBError("Failed to log search history") from exc
    return search_id


async def get_search_history(
    user_id: int,
    limit: int = 20,
    offset: int = 0,
    q: str | None = None,
) -> SearchHistoryPage:
    """Return count and page from one read-only repeatable-read snapshot."""
    pool = get_pool()
    try:
        async with (
            pool.acquire() as conn,
            conn.transaction(isolation="repeatable_read", readonly=True),
        ):
            if q is None:
                counts = await conn.fetchrow(_COUNT_HISTORY_SQL, user_id)
                rows = await conn.fetch(_PAGE_HISTORY_SQL, user_id, limit, offset)
            else:
                pattern = _literal_ilike_pattern(q)
                counts = await conn.fetchrow(_COUNT_FILTERED_HISTORY_SQL, user_id, pattern)
                rows = await conn.fetch(
                    _PAGE_FILTERED_HISTORY_SQL,
                    user_id,
                    pattern,
                    limit,
                    offset,
                )
    except asyncpg.PostgresError as exc:
        logger.error(
            "get_search_history_failed",
            user_id=user_id,
            error_type=type(exc).__name__,
        )
        raise DBError("Failed to fetch search history") from exc

    if counts is None:
        raise DBError("Failed to fetch search history counts")
    total = int(counts["total"])
    legacy_count = int(counts["legacy_level3_count"])
    if legacy_count:
        logger.warning(
            "legacy_search_history_excluded",
            user_id=user_id,
            count=legacy_count,
        )
    return SearchHistoryPage(
        items=[_history_entry(row) for row in rows],
        total=total,
        legacy_level3_count=legacy_count,
    )


async def bulk_soft_delete_search_entries(user_id: int, entry_ids: list[int]) -> int:
    """Soft-delete active owner-scoped rows in one atomic statement."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_BULK_DELETE_HISTORY_SQL, user_id, entry_ids)
    except asyncpg.PostgresError as exc:
        logger.error(
            "bulk_delete_search_history_failed",
            user_id=user_id,
            error_type=type(exc).__name__,
        )
        raise DBError("Failed to bulk-delete search history") from exc
    return len(rows)


async def soft_delete_search_entry(entry_id: int, user_id: int) -> bool:
    """Preserve the existing owner-scoped single-entry soft-delete behavior."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE public.search_history SET deleted_at = statement_timestamp() "
                "WHERE id = $1 AND user_id = $2",
                entry_id,
                user_id,
            )
    except asyncpg.PostgresError as exc:
        logger.error(
            "soft_delete_search_history_failed",
            entry_id=entry_id,
            error_type=type(exc).__name__,
        )
        raise DBError("Failed to soft-delete search history") from exc
    updated = int(result.split()[-1]) if result else 0
    return updated == 1


__all__ = [
    "bulk_soft_delete_search_entries",
    "get_search_history",
    "log_search",
    "soft_delete_search_entry",
]
