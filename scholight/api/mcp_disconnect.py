"""One owner for ASGI receive while MCP's JSON transport awaits a tool result."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

import anyio
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from scholight.web_extract.cancellation import finish_after_cancellation


@dataclass(frozen=True)
class MonitoredReceive:
    receive: Receive
    disconnected: Callable[[], Awaitable[None]]
    signal: Callable[[], None]
    is_disconnected: Callable[[], bool]


async def run_owned_request(
    app: ASGIApp,
    scope: Scope,
    monitored: MonitoredReceive,
    send: Send,
) -> None:
    async def safe_send(message: Message) -> None:
        if monitored.is_disconnected():
            return
        try:
            await send(message)
        except OSError:
            # ASGI reports a closed socket through send as well as receive. Let
            # the SDK finish its response so it can terminate the stateless session.
            monitored.signal()

    operation = asyncio.ensure_future(app(scope, monitored.receive, safe_send))
    try:
        await asyncio.shield(operation)
    except asyncio.CancelledError:
        monitored.signal()

        async def finish() -> None:
            with suppress(Exception):
                async with asyncio.timeout(2):
                    await operation

        cleanup = asyncio.create_task(finish(), name="mcp-request-cleanup")
        # MCP uses AnyIO level cancellation; also handle repeated asyncio Task.cancel.
        with anyio.CancelScope(shield=True):
            await finish_after_cancellation(cleanup)
        raise


@asynccontextmanager
async def monitor_disconnect(receive: Receive) -> AsyncIterator[MonitoredReceive]:
    # A single bounded queue preserves body chunks for the SDK. Concurrent receive
    # calls would race for those chunks and could corrupt JSON parsing.
    messages: asyncio.Queue[Message | Exception] = asyncio.Queue(maxsize=1)
    disconnected = asyncio.Event()

    async def pump() -> None:
        try:
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    disconnected.set()
                await messages.put(message)
                if message["type"] == "http.disconnect":
                    return
        except Exception as error:
            disconnected.set()
            await messages.put(error)

    async def forwarded() -> Message:
        if messages.empty() and disconnected.is_set():
            return {"type": "http.disconnect"}
        message = await messages.get()
        if isinstance(message, Exception):
            raise message
        return message

    reader = asyncio.create_task(pump(), name="mcp-disconnect-reader")

    async def wait() -> None:
        await disconnected.wait()

    try:
        yield MonitoredReceive(forwarded, wait, disconnected.set, disconnected.is_set)
    finally:
        disconnected.set()
        reader.cancel()
        cleanup = asyncio.gather(reader, return_exceptions=True)
        with anyio.CancelScope(shield=True):
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await finish_after_cancellation(cleanup)
                raise
