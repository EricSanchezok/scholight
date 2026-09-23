"""Lifecycle-owned periodic cache expiry without traffic-dependent retention."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress


@asynccontextmanager
async def cache_maintenance(
    prune: Callable[[], None], *, interval: float = 30
) -> AsyncIterator[None]:
    async def run() -> None:
        while True:
            await asyncio.sleep(interval)
            prune()

    task = asyncio.create_task(run(), name="extract-cache-expiry")
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
