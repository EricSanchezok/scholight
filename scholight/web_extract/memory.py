"""Container working-set hysteresis, independent of ECS reservation percentages."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from scholight.logging.emf import emit_emf
from scholight.web_extract.errors import ExtractError


@dataclass(frozen=True, slots=True)
class MemorySample:
    working_set: int
    anon: int
    file: int


def read_cgroup(root: Path = Path("/sys/fs/cgroup")) -> MemorySample:
    current = int((root / "memory.current").read_text())
    stats = dict(line.split() for line in (root / "memory.stat").read_text().splitlines())
    return MemorySample(
        working_set=max(0, current - int(stats["inactive_file"])),
        anon=int(stats["anon"]),
        file=int(stats["file"]),
    )


class MemoryGuard:
    def __init__(
        self,
        sample: Callable[[], MemorySample],
        reclaim: Callable[[], Awaitable[None]],
        *,
        high: int = 640 * 1024 * 1024,
        low: int = 512 * 1024 * 1024,
    ) -> None:
        self._sample = sample
        self._reclaim = reclaim
        self._high = high
        self._low = low
        self.paused = False
        self._recovery: asyncio.Task[None] | None = None

    def admit(self) -> None:
        if self.paused:
            raise ExtractError(
                code="extract_capacity_exceeded",
                message="Extraction memory capacity is exhausted.",
                status_code=503,
                retryable=True,
                retry_after=2,
            )

    async def _recover(self) -> None:
        async with asyncio.timeout(2):
            await self._reclaim()

    async def tick(self) -> None:
        try:
            sample = self._sample()
        except (OSError, ValueError, KeyError):
            self.paused = True
            emit_emf(service="extract", metrics={"MemorySampleFailure": (1, "Count")})
            return
        if sample.working_set >= self._high and (not self.paused or self._recovery is None):
            self.paused = True
            self._recovery = asyncio.create_task(self._recover(), name="extract-memory-reclaim")
            await asyncio.sleep(0)
        if (
            self._recovery is not None
            and self._recovery.done()
            and self._recovery.exception() is not None
        ):
            emit_emf(service="extract", metrics={"MemoryReclaimFailure": (1, "Count")})
            self._recovery = asyncio.create_task(self._recover(), name="extract-memory-reclaim")
            return
        if sample.working_set < self._low and (self._recovery is None or self._recovery.done()):
            self.paused = False
            self._recovery = None
        emit_emf(
            service="extract",
            metrics={
                "MemoryWorkingSet": (sample.working_set, "Bytes"),
                "MemoryAnon": (sample.anon, "Bytes"),
                "MemoryFile": (sample.file, "Bytes"),
                "MemoryAdmissionPaused": (int(self.paused), "Count"),
            },
        )

    @asynccontextmanager
    async def monitor(self) -> AsyncIterator[None]:
        async def run() -> None:
            while True:
                await self.tick()
                await asyncio.sleep(1)

        await self.tick()
        task = asyncio.create_task(run(), name="extract-memory-monitor")
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if self._recovery is not None:
                await self._recovery
