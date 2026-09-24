"""Bounded in-flight static work, with caller-independent cancellation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar
from uuid import uuid4

from scholight.web_extract.admission import capacity_error
from scholight.web_extract.cancellation import finish_after_cancellation
from scholight.web_extract.telemetry import current_trace, phase

T = TypeVar("T")


@dataclass(slots=True)
class _Flight(Generic[T]):
    task: asyncio.Task[T]
    work_id: str
    waiters: int = 0


async def _settle(tasks: list[asyncio.Task[T]]) -> None:
    cleanup = asyncio.gather(*tasks, return_exceptions=True)
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await finish_after_cancellation(cleanup)
        raise


class Singleflight(Generic[T]):
    def __init__(self, *, max_keys: int = 32, max_waiters: int = 8) -> None:
        self._max_keys = max_keys
        self._max_waiters = max_waiters
        self._flights: dict[str, _Flight[T]] = {}
        self._closed = False

    @property
    def active_keys(self) -> int:
        return len(self._flights)

    async def do(self, key: str, work: Callable[[str], Awaitable[T]]) -> T:
        if self._closed:
            raise capacity_error()
        flight = self._flights.get(key)
        joined = flight is not None
        if flight is None:
            if len(self._flights) >= self._max_keys:
                raise capacity_error()
            work_id = str(uuid4())

            async def execute() -> T:
                return await work(work_id)

            flight = _Flight(asyncio.create_task(execute()), work_id)
            self._flights[key] = flight
        elif flight.waiters == 0 or flight.waiters >= self._max_waiters:
            # A zero-waiter entry still owns cancellation cleanup, so cannot be rejoined.
            raise capacity_error()
        flight.waiters += 1
        if (trace := current_trace.get()) is not None:
            trace.static_work_id = flight.work_id
            trace.singleflight_joined = joined
        try:
            with phase("SingleflightWait"):
                return await asyncio.shield(flight.task)
        finally:
            flight.waiters -= 1
            if flight.waiters == 0:
                if not flight.task.done():
                    flight.task.cancel()
                try:
                    await _settle([flight.task])
                finally:
                    self._flights.pop(key, None)

    async def close(self) -> None:
        self._closed = True
        tasks = [flight.task for flight in self._flights.values()]
        for task in tasks:
            task.cancel()
        await _settle(tasks)
