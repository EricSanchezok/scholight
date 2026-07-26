"""Tests for passive public-search concurrency observation."""

from __future__ import annotations

import asyncio

import pytest

from scholight.api.search_in_flight import SearchInFlightTracker


@pytest.mark.asyncio
async def test_tracker_observes_concurrency_without_delaying_requests() -> None:
    tracker = SearchInFlightTracker()
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def search(strength: str) -> None:
        async with tracker.track(strength):  # type: ignore[arg-type]
            if tracker.snapshot().total == 2:
                both_started.set()
            await release.wait()

    tasks = [
        asyncio.create_task(search("standard")),
        asyncio.create_task(search("thorough")),
    ]
    await asyncio.wait_for(both_started.wait(), timeout=0.1)

    assert tracker.snapshot().total == 2
    assert tracker.snapshot().thorough == 1

    release.set()
    await asyncio.gather(*tasks)
    assert tracker.snapshot().total == 0
