"""Bounded public-search admission tests."""

from __future__ import annotations

import asyncio

import pytest

from scholight.api.search_capacity import SearchCapacityError, SearchCapacityGate


@pytest.mark.asyncio
async def test_zero_wait_admits_immediately_available_capacity() -> None:
    gate = SearchCapacityGate(total_limit=1, thorough_limit=1, wait_seconds=0)

    async with gate.admit("standard"):
        assert gate.snapshot().total_in_flight == 1


@pytest.mark.asyncio
async def test_total_capacity_rejects_without_waiting_for_running_search() -> None:
    gate = SearchCapacityGate(total_limit=1, thorough_limit=1, wait_seconds=0.01)

    async with gate.admit("standard"):
        with pytest.raises(SearchCapacityError):
            async with gate.admit("standard"):
                pytest.fail("capacity-rejected search must not enter")

    assert gate.snapshot().total_in_flight == 0


@pytest.mark.asyncio
async def test_thorough_capacity_is_independent_from_total_capacity() -> None:
    gate = SearchCapacityGate(total_limit=2, thorough_limit=1, wait_seconds=0.01)

    async with gate.admit("thorough"):
        async with gate.admit("standard"):
            with pytest.raises(SearchCapacityError):
                async with gate.admit("thorough"):
                    pytest.fail("second thorough search must not enter")

    assert gate.snapshot().thorough_in_flight == 0


@pytest.mark.asyncio
async def test_exception_releases_all_capacity_slots() -> None:
    gate = SearchCapacityGate(total_limit=1, thorough_limit=1, wait_seconds=0.01)

    with pytest.raises(RuntimeError, match="boom"):
        async with gate.admit("thorough"):
            raise RuntimeError("boom")

    async with gate.admit("thorough"):
        assert gate.snapshot().total_in_flight == 1
        assert gate.snapshot().thorough_in_flight == 1


@pytest.mark.asyncio
async def test_cancellation_while_waiting_does_not_leak_capacity() -> None:
    gate = SearchCapacityGate(total_limit=1, thorough_limit=1, wait_seconds=1)

    async with gate.admit("standard"):
        waiter = asyncio.create_task(gate.acquire("standard"))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter

    async with gate.admit("standard"):
        assert gate.snapshot().total_in_flight == 1
