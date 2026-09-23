from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from scholight.web_extract.errors import ExtractError
from scholight.web_extract.memory import MemoryGuard, MemorySample, read_cgroup


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
