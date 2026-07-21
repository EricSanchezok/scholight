"""Managed background persistence for successful authenticated searches."""

from __future__ import annotations

import asyncio
import time

import structlog

from scholight.db.client import DBError
from scholight.db.queries_history import log_search

logger = structlog.get_logger(__name__)
_history_tasks: set[asyncio.Task[None]] = set()


async def _write_search_history(
    *,
    request_id: str,
    user_id: int,
    query_text: str,
    level: int,
    strength: str,
    filters: dict[str, object] | None,
    result_count: int,
    elapsed_ms: float,
) -> None:
    started = time.perf_counter()
    try:
        await log_search(
            user_id=user_id,
            query_text=query_text,
            level=level,
            strategy=None,
            filters=filters,
            num_results=result_count,
            response_time_ms=elapsed_ms,
        )
    except Exception as exc:
        logger.warning(
            "search_history_write_failed",
            request_id=request_id,
            user_id=user_id,
            strength=strength,
            result_count=result_count,
            error_type=type(exc).__name__,
            retryable=isinstance(exc, DBError),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )


def schedule_search_history_write(
    *,
    request_id: str,
    user_id: int,
    query_text: str,
    level: int,
    strength: str,
    filters: dict[str, object] | None,
    result_count: int,
    elapsed_ms: float,
) -> None:
    """Schedule a self-contained history write whose exception is consumed."""
    task = asyncio.create_task(
        _write_search_history(
            request_id=request_id,
            user_id=user_id,
            query_text=query_text,
            level=level,
            strength=strength,
            filters=filters,
            result_count=result_count,
            elapsed_ms=elapsed_ms,
        )
    )
    _history_tasks.add(task)
    task.add_done_callback(_history_tasks.discard)


async def drain_search_history_tasks() -> None:
    """Wait for all currently pending history writes during API shutdown."""
    while _history_tasks:
        await asyncio.gather(*tuple(_history_tasks), return_exceptions=True)


__all__ = ["drain_search_history_tasks", "schedule_search_history_write"]
