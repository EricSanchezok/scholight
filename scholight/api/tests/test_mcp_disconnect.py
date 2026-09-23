from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sanchezcloud_identity.models.user import UserRecord
from starlette.types import Message, Scope

from scholight.api.deps import SearchActor
from scholight.api.extract_execution import PublicExtractError
from scholight.api.mcp_server import create_mcp_app
from scholight.models.web_extract import ExtractRequest


@pytest.mark.asyncio
@pytest.mark.parametrize("leave", ["disconnect", "cancel", "repeat_cancel", "send_failure"])
async def test_real_mcp_json_transport_cancels_extract_when_http_client_leaves(
    active_user: UserRecord,
    leave: str,
) -> None:
    server, app = create_mcp_app()
    messages: asyncio.Queue[Message] = asyncio.Queue()
    started, stopped = asyncio.Event(), asyncio.Event()
    finish_document, finish_cleanup = asyncio.Event(), asyncio.Event()
    send_failed = False
    actor = SearchActor(user=active_user, actor_type="access_key", access_key_id=uuid4())
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "root_path": "",
        "query_string": b"",
        "server": ("test", 80),
        "client": ("192.0.2.1", 12345),
        "headers": [
            (b"host", b"test"),
            (b"accept", b"application/json, text/event-stream"),
            (b"content-type", b"application/json"),
            (b"mcp-protocol-version", b"2025-11-25"),
            (b"authorization", b"Bearer sk_live_fixture"),
        ],
    }
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "extract_url", "arguments": {"url": "https://example.org"}},
        }
    ).encode()
    # Exercise multipart ASGI body delivery before the disconnect, not an HTTPX
    # transport that waits for the response before it reports disconnection.
    await messages.put({"type": "http.request", "body": payload[:20], "more_body": True})
    await messages.put({"type": "http.request", "body": payload[20:], "more_body": False})

    async def slow_document(_request: ExtractRequest) -> None:
        started.set()
        try:
            await finish_document.wait()
            raise PublicExtractError(
                status_code=502,
                code="extract_upstream_error",
                message="Fixture document failed.",
                retryable=False,
            )
        finally:
            stopped.set()
            if leave == "repeat_cancel":
                await finish_cleanup.wait()

    async def send(_message: Message) -> None:
        nonlocal send_failed
        if leave == "send_failure":
            send_failed = True
            raise OSError("client connection closed")

    with (
        patch(
            "scholight.api.mcp_server.resolve_access_key_search_actor",
            AsyncMock(return_value=actor),
        ),
        patch("scholight.api.extract_execution._request_document", slow_document),
        patch("scholight.api.extract_execution.log_completion") as completion,
    ):
        async with server.session_manager.run():
            task = asyncio.ensure_future(app(scope, messages.get, send))
            try:
                await asyncio.wait_for(started.wait(), 1)
                if leave in {"cancel", "repeat_cancel"}:
                    task.cancel()
                elif leave == "send_failure":
                    finish_document.set()
                else:
                    await messages.put({"type": "http.disconnect"})
                await asyncio.wait_for(stopped.wait(), 0.5)
                if leave == "repeat_cancel":
                    task.cancel()
                    finish_cleanup.set()
                result = await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 1)
                if leave in {"cancel", "repeat_cancel"}:
                    assert isinstance(result[0], asyncio.CancelledError)
                else:
                    assert result[0] is None
                assert send_failed == (leave == "send_failure")
                # The stateless SDK session must terminate before application shutdown.
                # Otherwise each cancelled HTTP request retains an idle server task.
                await asyncio.sleep(0.01)
                assert not any(
                    "run_stateless_server" in candidate.get_name()
                    for candidate in asyncio.all_tasks()
                    if not candidate.done()
                )
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        assert completion.call_count == 1
        assert completion.call_args.kwargs["outcome"] == (
            "error_extract_upstream_error" if leave == "send_failure" else "cancelled"
        )
