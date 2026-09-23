from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scholight.web_extract.errors import ExtractError
from scholight.web_extract.memory import MemoryGuard, MemorySample, read_cgroup
from scholight.web_extract.reservations import MemoryBudget, MemoryModel, StageCost


def test_working_set_excludes_inactive_file_cache(tmp_path) -> None:
    (tmp_path / "memory.current").write_text("800")
    (tmp_path / "memory.stat").write_text("anon 500\nfile 300\ninactive_file 200\n")
    sample = read_cgroup(tmp_path)
    assert (sample.working_set, sample.anon, sample.file) == (600, 500, 300)


@pytest.mark.asyncio
async def test_memory_hysteresis_reclaims_once_and_resumes_only_below_low_water() -> None:
    reclaim = AsyncMock()
    sample = MemorySample(working_set=641, anon=500, file=141)
    guard = MemoryGuard(lambda: sample, reclaim, high=640, low=512)
    await guard.tick()
    with pytest.raises(ExtractError) as error:
        guard.admit()
    assert error.value.code == "extract_capacity_exceeded"
    sample = MemorySample(working_set=600, anon=500, file=100)
    await guard.tick()
    assert guard.paused
    sample = MemorySample(working_set=511, anon=500, file=11)
    await guard.tick()
    guard.admit()
    reclaim.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_memory_measurement_stops_admission() -> None:
    def unavailable():
        raise OSError("cgroup read failed")

    guard = MemoryGuard(unavailable, AsyncMock())
    await guard.tick()
    with pytest.raises(ExtractError):
        guard.admit()


@pytest.mark.asyncio
async def test_reservation_pressure_waits_for_idle_and_recovers_below_high_water() -> None:
    working = 150

    async def reclaim():
        nonlocal working
        # Pause admission before awaiting any cleanup, including queued grants.
        with pytest.raises(ExtractError):
            guard.admit()
        working = 100

    cleanup = AsyncMock(side_effect=reclaim)
    guard = MemoryGuard(
        lambda: MemorySample(working, working, 0),
        cleanup,
        high=200,
        low=120,
        can_reclaim=lambda: budget.reserved_bytes == 0,
    )
    budget = MemoryBudget(
        lambda: working,
        guard.admit,
        high=200,
        model=MemoryModel(download=StageCost(10, 0), browser=StageCost(80, 0)),
        on_pressure=guard.request_reclaim,
    )
    lease = budget.lease()
    lease.transfer("download")
    with pytest.raises(ExtractError):
        lease.transfer("browser")
    await guard.tick()
    cleanup.assert_not_awaited()
    assert not guard.paused and budget.reserved_bytes == 10
    lease.close()
    await guard.tick()
    assert guard.paused
    await guard.tick()
    guard.admit()
    cleanup.assert_awaited_once()
    later = budget.lease()
    later.transfer("browser")
    later.close()


@pytest.mark.asyncio
async def test_pressure_recovery_is_throttled_but_physical_high_water_is_not() -> None:
    now = 0.0
    sample = MemorySample(100, 100, 0)
    reclaim = AsyncMock()
    guard = MemoryGuard(
        lambda: sample,
        reclaim,
        high=200,
        low=120,
        can_reclaim=lambda: True,
        clock=lambda: now,
    )
    guard.request_reclaim()
    await guard.tick()
    await guard.tick()
    reclaim.assert_awaited_once()
    now = 1
    guard.request_reclaim()
    await guard.tick()
    reclaim.assert_awaited_once()
    now = 30
    await guard.tick()
    await guard.tick()
    assert reclaim.await_count == 2
    now = 31
    sample = MemorySample(201, 201, 0)
    await guard.tick()
    assert reclaim.await_count == 3 and guard.paused
