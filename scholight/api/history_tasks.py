"""Bounded single-writer persistence for successful authenticated searches."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import structlog

from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.queries_history import log_search
from scholight.logging.emf import emit_emf

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _HistoryWrite:
    request_id: str
    user_id: int
    query_text: str
    strength: str
    filters: dict[str, object] | None
    result_count: int
    elapsed_ms: float


_history_queue: asyncio.Queue[_HistoryWrite] | None = None
_history_worker: asyncio.Task[None] | None = None


async def _write_search_history(item: _HistoryWrite) -> None:
    started = time.perf_counter()
    try:
        await log_search(
            user_id=item.user_id,
            query_text=item.query_text,
            strength=item.strength,
            filters=item.filters,
            result_count=item.result_count,
            response_time_ms=item.elapsed_ms,
        )
    except Exception as exc:
        logger.warning(
            "search_history_write_failed",
            request_id=item.request_id,
            user_id=item.user_id,
            strength=item.strength,
            result_count=item.result_count,
            error_type=type(exc).__name__,
            retryable=isinstance(exc, DBError),
            duration_ms=round((time.perf_counter() - started) * 1000, 2),
        )


async def _run_history_worker(queue: asyncio.Queue[_HistoryWrite]) -> None:
    while True:
        item = await queue.get()
        try:
            await _write_search_history(item)
        finally:
            queue.task_done()


def _ensure_history_worker() -> asyncio.Queue[_HistoryWrite]:
    global _history_queue, _history_worker
    loop = asyncio.get_running_loop()
    if _history_worker is not None and _history_worker.get_loop() is not loop:
        _history_queue = None
        _history_worker = None
    if _history_queue is None:
        _history_queue = asyncio.Queue(maxsize=settings.background_queue_max_size)
    if _history_worker is None or _history_worker.done():
        _history_worker = asyncio.create_task(
            _run_history_worker(_history_queue),
            name="scholight-history-writer",
        )
    return _history_queue


def schedule_search_history_write(
    *,
    request_id: str,
    user_id: int,
    query_text: str,
    strength: str,
    filters: dict[str, object] | None,
    result_count: int,
    elapsed_ms: float,
) -> bool:
    """Enqueue a best-effort write without creating one task per request."""
    queue = _ensure_history_worker()
    try:
        queue.put_nowait(
            _HistoryWrite(
                request_id=request_id,
                user_id=user_id,
                query_text=query_text,
                strength=strength,
                filters=filters,
                result_count=result_count,
                elapsed_ms=elapsed_ms,
            )
        )
    except asyncio.QueueFull:
        logger.warning(
            "search_history_queue_dropped",
            request_id=request_id,
            queue_depth=queue.qsize(),
        )
        emit_emf(
            service="api",
            outcome="dropped",
            metrics={
                "HistoryQueueDepth": (queue.qsize(), "Count"),
                "HistoryQueueDropped": (1, "Count"),
            },
        )
        return False
    emit_emf(
        service="api",
        outcome="queued",
        metrics={"HistoryQueueDepth": (queue.qsize(), "Count")},
    )
    return True


async def drain_search_history_tasks() -> None:
    """Flush the queue and stop its single worker during API shutdown."""
    global _history_queue, _history_worker
    queue, worker = _history_queue, _history_worker
    if worker is not None and worker.get_loop() is not asyncio.get_running_loop():
        _history_queue = None
        _history_worker = None
        return
    if queue is not None:
        await queue.join()
    if worker is not None:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    _history_queue = None
    _history_worker = None


def history_queue_depth() -> int:
    return 0 if _history_queue is None else _history_queue.qsize()


__all__ = [
    "drain_search_history_tasks",
    "history_queue_depth",
    "schedule_search_history_write",
]
