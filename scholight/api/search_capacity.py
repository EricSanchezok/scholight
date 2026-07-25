"""Process-local admission control for bounded public-search execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

from scholight.config import settings

SearchStrength = Literal["standard", "thorough"]


class SearchCapacityError(Exception):
    """Raised when a search cannot be admitted within the short queue budget."""


@dataclass(frozen=True, slots=True)
class SearchCapacitySnapshot:
    total_in_flight: int
    thorough_in_flight: int


class SearchCapacityGate:
    """Bound total work and the more expensive thorough subset."""

    def __init__(self, *, total_limit: int, thorough_limit: int, wait_seconds: float) -> None:
        if total_limit < 1 or thorough_limit < 1 or thorough_limit > total_limit:
            raise ValueError("capacity limits must satisfy 1 <= thorough <= total")
        if wait_seconds < 0:
            raise ValueError("wait_seconds must be non-negative")
        self._total = asyncio.Semaphore(total_limit)
        self._thorough = asyncio.Semaphore(thorough_limit)
        self._wait_seconds = wait_seconds
        self._total_in_flight = 0
        self._thorough_in_flight = 0

    async def _acquire(self, semaphore: asyncio.Semaphore) -> None:
        if self._wait_seconds == 0:
            if semaphore.locked():
                raise SearchCapacityError
            await semaphore.acquire()
            return
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=self._wait_seconds)
        except TimeoutError as exc:
            raise SearchCapacityError from exc

    async def acquire(self, strength: SearchStrength) -> None:
        """Acquire all slots for one search, releasing partial acquisition on failure."""
        await self._acquire(self._total)
        total_acquired = True
        try:
            if strength == "thorough":
                await self._acquire(self._thorough)
            elif strength != "standard":
                raise ValueError("strength must be standard or thorough")
        except BaseException:
            if total_acquired:
                self._total.release()
            raise
        self._total_in_flight += 1
        if strength == "thorough":
            self._thorough_in_flight += 1

    def release(self, strength: SearchStrength) -> None:
        """Release exactly the slots acquired for one admitted search."""
        self._total_in_flight -= 1
        self._total.release()
        if strength == "thorough":
            self._thorough_in_flight -= 1
            self._thorough.release()

    @asynccontextmanager
    async def admit(self, strength: SearchStrength) -> AsyncIterator[None]:
        await self.acquire(strength)
        try:
            yield
        finally:
            self.release(strength)

    def snapshot(self) -> SearchCapacitySnapshot:
        return SearchCapacitySnapshot(
            total_in_flight=self._total_in_flight,
            thorough_in_flight=self._thorough_in_flight,
        )


_gate: SearchCapacityGate | None = None
_gate_config: tuple[int, int, float] | None = None


def get_search_capacity_gate() -> SearchCapacityGate:
    """Return the gate for the current settings, rebuilding it after test overrides."""
    global _gate, _gate_config
    config = (
        settings.search_max_in_flight,
        settings.search_thorough_max_in_flight,
        settings.search_capacity_wait_ms / 1000,
    )
    if _gate is None or _gate_config != config:
        _gate = SearchCapacityGate(
            total_limit=config[0],
            thorough_limit=config[1],
            wait_seconds=config[2],
        )
        _gate_config = config
    return _gate


def reset_search_capacity_gate() -> None:
    """Drop process-local admission state during startup and isolated tests."""
    global _gate, _gate_config
    _gate = None
    _gate_config = None


__all__ = [
    "SearchCapacityError",
    "SearchCapacityGate",
    "SearchCapacitySnapshot",
    "get_search_capacity_gate",
    "reset_search_capacity_gate",
]
