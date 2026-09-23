from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from scholight.web_extract.errors import ExtractError
from scholight.web_extract.memory import MemoryGuard, MemorySample, read_cgroup


def test_working_set_excludes_inactive_file_cache(tmp_path) -> None:
    (tmp_path / "memory.current").write_text("800")
    (tmp_path / "memory.stat").write_text("anon 500\nfile 300\ninactive_file 200\n")
    (tmp_path / "memory.events").write_text("low 0\nhigh 0\nmax 8\noom 3\noom_kill 2\n")
    sample = read_cgroup(tmp_path)
    assert (sample.working_set, sample.anon, sample.file, sample.oom_kills) == (600, 500, 300, 2)


@pytest.mark.asyncio
async def test_worker_oom_kill_is_reported_after_working_set_has_recovered() -> None:
    guard = MemoryGuard(lambda: MemorySample(100, 100, 0, oom_kills=2), AsyncMock())
    with patch("scholight.web_extract.memory.emit_emf") as emit:
        await guard.tick()
    assert emit.call_args.kwargs["metrics"]["MemoryOOMKills"] == (2, "Count")


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
