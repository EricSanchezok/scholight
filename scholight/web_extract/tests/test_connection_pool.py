from __future__ import annotations

import json

import aiohttp
import pytest
from aiohttp import web

from scholight.web_extract.errors import ExtractError
from scholight.web_extract.fetcher import HttpFetcher
from scholight.web_extract.tests.test_fetcher import _allow_test_target, _request, _server

pytestmark = pytest.mark.asyncio


async def test_external_pool_reuses_transport_without_reusing_caller_or_server_cookies() -> None:
    transports = []

    async def handler(request: web.Request) -> web.Response:
        transports.append(request.transport)
        response = web.json_response(
            {
                "cookie": request.headers.get("Cookie"),
                "authorization": request.headers.get("Authorization"),
            }
        )
        response.set_cookie("server_session", "private")
        return response

    async with _server(handler) as base:
        base = base.replace("127.0.0.1", "localhost")
        fetcher = HttpFetcher(
            validator=_allow_test_target, resolver=aiohttp.DefaultResolver(), reuse_connections=True
        )
        try:
            await fetcher.fetch(
                _request(base, headers={"Authorization": "private"}, cookies={"caller": "private"})
            )
            second = await fetcher.fetch(_request(base))
            third = await fetcher.fetch(_request(base))
            assert json.loads(second.body) == {"cookie": None, "authorization": None}
            assert json.loads(third.body) == {"cookie": None, "authorization": None}
            assert transports[0] is not transports[1]
            assert transports[1] is transports[2]
        finally:
            await fetcher.close()


async def test_reused_connections_still_validate_every_requested_target() -> None:
    calls = 0

    async def validator(_url):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise ExtractError(
                code="blocked_target", message="blocked", status_code=403, retryable=False
            )

    async def handler(_request):
        return web.Response(text="ok")

    async with _server(handler) as base:
        fetcher = HttpFetcher(
            validator=validator, resolver=aiohttp.DefaultResolver(), reuse_connections=True
        )
        try:
            await fetcher.fetch(_request(base))
            with pytest.raises(ExtractError, match="blocked"):
                await fetcher.fetch(_request(base))
        finally:
            await fetcher.close()


async def test_cookie_state_is_retained_only_inside_one_redirect_chain() -> None:
    async def handler(request):
        if request.path == "/seed":
            response = web.HTTPFound("/end")
            response.set_cookie("state", "within-request")
            raise response
        return web.Response(text=request.cookies.get("state", "none"))

    async with _server(handler) as base:
        base = base.replace("127.0.0.1", "localhost")
        fetcher = HttpFetcher(
            validator=_allow_test_target, resolver=aiohttp.DefaultResolver(), reuse_connections=True
        )
        try:
            first = await fetcher.fetch(_request(base + "/seed"))
            second = await fetcher.fetch(_request(base + "/end"))
            assert first.body == b"within-request" and second.body == b"none"
        finally:
            await fetcher.close()
