"""Managed best-effort usage-event persistence."""

from __future__ import annotations

import asyncio

import structlog

from scholight.db.queries_usage import UsageEvent, insert_usage_event

logger = structlog.get_logger(__name__)
_usage_tasks: set[asyncio.Task[None]] = set()


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


def schedule_usage_event(event: UsageEvent) -> None:
    task = asyncio.create_task(_write_usage_event(event))
    _usage_tasks.add(task)
    task.add_done_callback(_usage_tasks.discard)


async def drain_usage_tasks() -> None:
    while _usage_tasks:
        await asyncio.gather(*tuple(_usage_tasks), return_exceptions=True)


__all__ = ["drain_usage_tasks", "schedule_usage_event"]
