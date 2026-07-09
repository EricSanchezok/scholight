"""History query functions — search history logging and retrieval."""

from __future__ import annotations

import json

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool
from scholight.models.history import SearchHistoryEntry

logger = structlog.get_logger(__name__)


# ── Search history ─────────────────────────────────────────────────────────


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
                "INSERT INTO search_history "
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
        logger.error("log_search failed", user_id=user_id, error=str(exc))
        raise DBError(f"Failed to log search: {exc}") from exc
    return search_id


async def get_search_history(
    user_id: int, limit: int = 20, offset: int = 0
) -> list[SearchHistoryEntry]:
    """Return the most recent (non-deleted) search entries for *user_id*."""
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, query_text, level, strategy, filters, "
                "num_results, response_time_ms, created_at "
                "FROM search_history "
                "WHERE user_id = $1 AND deleted_at IS NULL "
                "ORDER BY created_at DESC "
                "LIMIT $2 OFFSET $3",
                user_id,
                limit,
                offset,
            )
    except asyncpg.PostgresError as exc:
        logger.error("get_search_history failed", user_id=user_id, error=str(exc))
        raise DBError(f"Failed to fetch search history: {exc}") from exc
    results: list[SearchHistoryEntry] = []
    for r in rows:
        raw: dict[str, object] = dict(r)
        filters_val = raw.get("filters")
        if filters_val is not None and isinstance(filters_val, str):
            raw["filters"] = json.loads(filters_val)
        results.append(SearchHistoryEntry(**raw))  # type: ignore[arg-type]
    return results


async def soft_delete_search_entry(entry_id: int, user_id: int) -> bool:
    """Soft-delete a search history entry belonging to *user_id*.

    Returns ``True`` if a row was updated, ``False`` otherwise.
    """
    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(
                "UPDATE search_history SET deleted_at = now() WHERE id = $1 AND user_id = $2",
                entry_id,
                user_id,
            )
    except asyncpg.PostgresError as exc:
        logger.error("soft_delete_search_entry failed", entry_id=entry_id, error=str(exc))
        raise DBError(f"Failed to soft-delete search entry: {exc}") from exc
    updated = int(result.split()[-1]) if result else 0
    return updated == 1
