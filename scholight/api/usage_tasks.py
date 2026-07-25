"""Bounded single-writer persistence for best-effort usage analytics."""

from __future__ import annotations

import asyncio

import structlog

from scholight.config import settings
from scholight.db.queries_usage import UsageEvent, insert_usage_event
from scholight.logging.emf import emit_emf

logger = structlog.get_logger(__name__)
_usage_queue: asyncio.Queue[UsageEvent] | None = None
_usage_worker: asyncio.Task[None] | None = None


async def _write_usage_event(event: UsageEvent) -> None:
    try:
        await insert_usage_event(event)
    except Exception as exc:
        logger.warning(
            "usage_event_write_failed",
            request_id=event.request_id,
            user_id=event.user_id,
            outcome=event.outcome,
            error_type=type(exc).__name__,
        )


async def _run_usage_worker(queue: asyncio.Queue[UsageEvent]) -> None:
    while True:
        event = await queue.get()
        try:
            await _write_usage_event(event)
        finally:
            queue.task_done()


def _ensure_usage_worker() -> asyncio.Queue[UsageEvent]:
    global _usage_queue, _usage_worker
    loop = asyncio.get_running_loop()
    if _usage_worker is not None and _usage_worker.get_loop() is not loop:
        _usage_queue = None
        _usage_worker = None
    if _usage_queue is None:
        _usage_queue = asyncio.Queue(maxsize=settings.background_queue_max_size)
    if _usage_worker is None or _usage_worker.done():
        _usage_worker = asyncio.create_task(
            _run_usage_worker(_usage_queue),
            name="scholight-usage-writer",
        )
    return _usage_queue


def schedule_usage_event(event: UsageEvent) -> bool:
    """Enqueue one best-effort event and fail open when analytics is saturated."""
    queue = _ensure_usage_worker()
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        logger.warning(
            "usage_event_queue_dropped",
            request_id=event.request_id,
            queue_depth=queue.qsize(),
        )
        emit_emf(
            service="api",
            outcome="dropped",
            metrics={
                "UsageQueueDepth": (queue.qsize(), "Count"),
                "UsageQueueDropped": (1, "Count"),
            },
        )
        return False
    emit_emf(
        service="api",
        outcome="queued",
        metrics={"UsageQueueDepth": (queue.qsize(), "Count")},
    )
    return True


async def drain_usage_tasks() -> None:
    """Flush the queue and stop its single worker during API shutdown."""
    global _usage_queue, _usage_worker
    queue, worker = _usage_queue, _usage_worker
    if worker is not None and worker.get_loop() is not asyncio.get_running_loop():
        _usage_queue = None
        _usage_worker = None
        return
    if queue is not None:
        await queue.join()
    if worker is not None:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    _usage_queue = None
    _usage_worker = None


def usage_queue_depth() -> int:
    return 0 if _usage_queue is None else _usage_queue.qsize()


__all__ = ["drain_usage_tasks", "schedule_usage_event", "usage_queue_depth"]
