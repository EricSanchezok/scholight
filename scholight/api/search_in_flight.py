"""Passive process-local observation of concurrent public searches."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Literal

SearchStrength = Literal["standard", "thorough"]


@dataclass(frozen=True, slots=True)
class SearchInFlightSnapshot:
    total: int
    thorough: int


class SearchInFlightTracker:
    """Count active searches without queueing, rejecting, or delaying requests."""

    def __init__(self) -> None:
        self._total = 0
        self._thorough = 0

    @asynccontextmanager
    async def track(self, strength: SearchStrength) -> AsyncIterator[None]:
        if strength not in {"standard", "thorough"}:
            raise ValueError("strength must be standard or thorough")
        self._total += 1
        if strength == "thorough":
            self._thorough += 1
        try:
            yield
        finally:
            self._total -= 1
            if strength == "thorough":
                self._thorough -= 1

    def snapshot(self) -> SearchInFlightSnapshot:
        return SearchInFlightSnapshot(total=self._total, thorough=self._thorough)


_tracker: SearchInFlightTracker | None = None


def get_search_in_flight_tracker() -> SearchInFlightTracker:
    global _tracker
    if _tracker is None:
        _tracker = SearchInFlightTracker()
    return _tracker


def reset_search_in_flight_tracker() -> None:
    global _tracker
    _tracker = None


__all__ = [
    "SearchInFlightSnapshot",
    "SearchInFlightTracker",
    "get_search_in_flight_tracker",
    "reset_search_in_flight_tracker",
]
