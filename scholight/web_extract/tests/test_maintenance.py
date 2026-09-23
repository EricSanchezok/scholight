from __future__ import annotations

import asyncio

import pytest

from scholight.web_extract.maintenance import cache_maintenance


@pytest.mark.asyncio
async def test_idle_cache_is_swept_and_task_stops_at_shutdown() -> None:
    swept = asyncio.Event()
    calls = 0

    def prune() -> None:
        nonlocal calls
        calls += 1
        swept.set()

    async with cache_maintenance(prune, interval=0.001):
        await asyncio.wait_for(swept.wait(), timeout=1)
    at_shutdown = calls
    await asyncio.sleep(0.005)
    assert calls == at_shutdown
