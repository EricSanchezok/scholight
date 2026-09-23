"""FIFO execution permits with fixed queue length and waiting deadlines."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal

from scholight.logging.emf import emit_emf
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.telemetry import current_trace, phase


def capacity_error() -> ExtractError:
    return ExtractError(
        code="extract_capacity_exceeded",
        message="Extraction capacity is temporarily exhausted.",
        status_code=503,
        retryable=True,
        retry_after=2,
    )


class BoundedGate:
    def __init__(
        self,
        name: Literal["Download", "Parse", "Browser"],
        *,
        capacity: int,
        max_waiters: int,
        wait_seconds: float,
    ) -> None:
        self._name = name
        self._capacity = capacity
        self._max_waiters = max_waiters
        self._wait_seconds = wait_seconds
        self._active = 0
        self._queue: deque[asyncio.Future[None]] = deque()

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return len(self._queue)

    def _metrics(self, *, rejected: bool = False) -> None:
        emit_emf(
            service="extract",
            metrics={
                f"{self._name}Active": (self.active, "Count"),
                f"{self._name}QueueDepth": (self.waiting, "Count"),
                f"{self._name}QueueRejected": (int(rejected), "Count"),
            },
        )

    async def acquire(self) -> None:
        if self._active < self._capacity and not self._queue:
            self._active += 1
            self._metrics()
            return
        if len(self._queue) >= self._max_waiters:
            self._metrics(rejected=True)
            raise capacity_error()
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._queue.append(future)
        self._metrics()
        timeout = self._wait_seconds
        if (trace := current_trace.get()) is not None:
            timeout = min(timeout, max(0, trace.remaining() - 2))
        try:
            with phase(f"{self._name}QueueLatency"):
                async with asyncio.timeout(timeout):
                    await future
        except BaseException as error:
            if future.done() and not future.cancelled():
                # Cancellation can race a grant before the waiter resumes.
                self.release()
            elif future in self._queue:
                self._queue.remove(future)
            self._metrics(rejected=isinstance(error, TimeoutError))
            if isinstance(error, TimeoutError):
                raise capacity_error() from error
            raise

    def release(self) -> None:
        self._active -= 1
        while self._queue:
            future = self._queue.popleft()
            if not future.done():
                self._active += 1
                future.set_result(None)
                break
        self._metrics()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        await self.acquire()
        try:
            yield
        finally:
            self.release()


class Permit:
    """Transfer a stage permit to a result without allowing duplicate release."""

    def __init__(self, gate: BoundedGate) -> None:
        self._gate: BoundedGate | None = gate

    def close(self) -> None:
        if self._gate is not None:
            gate, self._gate = self._gate, None
            gate.release()
