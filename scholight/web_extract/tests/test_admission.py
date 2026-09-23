from __future__ import annotations

import asyncio

import pytest

from scholight.web_extract.admission import BoundedGate
from scholight.web_extract.errors import ExtractError

pytestmark = pytest.mark.asyncio


async def test_gate_bounds_waiters_and_grants_in_fifo_order() -> None:
    gate = BoundedGate("Parse", capacity=1, max_waiters=4, wait_seconds=2)
    await gate.acquire()
    granted = []

    async def waiting(index):
        async with gate.slot():
            granted.append(index)

    tasks = [asyncio.create_task(waiting(i)) for i in range(4)]
    await asyncio.sleep(0)
    with pytest.raises(ExtractError) as error:
        await gate.acquire()
    assert error.value.code == "extract_capacity_exceeded"
    assert (gate.active, gate.waiting) == (1, 4)
    gate.release()
    await asyncio.gather(*tasks)
    assert granted == list(range(4))
    assert (gate.active, gate.waiting) == (0, 0)


async def test_cancelling_a_granted_waiter_returns_its_permit() -> None:
    gate = BoundedGate("Download", capacity=1, max_waiters=8, wait_seconds=2)
    await gate.acquire()
    pending = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    gate.release()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (gate.active, gate.waiting) == (0, 0)


async def test_wait_timeout_removes_the_waiter_without_releasing_active_work() -> None:
    gate = BoundedGate("Browser", capacity=1, max_waiters=2, wait_seconds=0.01)
    async with gate.slot():
        with pytest.raises(ExtractError):
            await gate.acquire()
        assert (gate.active, gate.waiting) == (1, 0)


async def test_cancelled_waiter_does_not_change_other_waiters_order() -> None:
    gate = BoundedGate("Parse", capacity=1, max_waiters=4, wait_seconds=2)
    await gate.acquire()
    cancelled = asyncio.create_task(gate.acquire())
    remaining = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    gate.release()
    await remaining
    assert (gate.active, gate.waiting) == (1, 0)
    gate.release()


async def test_release_can_skip_a_cancelled_waiter_before_it_resumes() -> None:
    gate = BoundedGate("Parse", capacity=1, max_waiters=4, wait_seconds=2)
    await gate.acquire()
    pending = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    pending.cancel()
    gate.release()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert (gate.active, gate.waiting) == (0, 0)
