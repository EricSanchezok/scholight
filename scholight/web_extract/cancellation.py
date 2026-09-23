"""Cancel owned work when its caller leaves, then await resource cleanup."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")


class ClientDisconnectedError(Exception):
    """A normal client lifecycle event, not an unhandled ASGI cancellation."""


async def finish_after_cancellation(task: asyncio.Future[T]) -> T:
    """Finish already-owned cleanup despite repeated cancellation of its caller.

    Only use after catching cancellation, then re-raise the original cancellation.
    The owned operation must have its own cleanup deadline where appropriate.
    """
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


async def until_disconnect(
    work: Awaitable[T],
    disconnected: Callable[[], Awaitable[None]],
) -> T:
    operation = asyncio.ensure_future(work)
    watcher = asyncio.ensure_future(disconnected())
    try:
        done, _ = await asyncio.wait({operation, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if operation in done:
            return await operation
        raise ClientDisconnectedError
    finally:
        operation.cancel()
        watcher.cancel()
        await asyncio.gather(operation, watcher, return_exceptions=True)
