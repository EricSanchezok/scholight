from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.engine import ExtractInput
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.fetcher import HttpFetcher


@asynccontextmanager
async def _server(handler: web.RequestHandler) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server is not None else []
    port = sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


async def _allow_test_target(_url: str) -> None:
    return None


def _request(url: str, **overrides: object) -> ExtractInput:
    values: dict[str, object] = {
        "url": url,
        "render": RenderMode.NEVER,
        "output": ExtractResponseFormat.MAIN_MARKDOWN,
    }
    values.update(overrides)
    return ExtractInput(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_http_fetcher_forwards_target_headers_and_cookies() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "authorization": request.headers.get("Authorization"),
                "cookie": request.headers.get("Cookie"),
            }
        )

    async with _server(handler) as base_url:
        fetcher = HttpFetcher(
            validator=_allow_test_target,
            resolver=aiohttp.DefaultResolver(),
            max_download_bytes=10_000,
        )
        result = await fetcher.fetch(
            _request(
                base_url,
                headers={"Authorization": "Bearer target"},
                cookies={"session": "cookie-value"},
            )
        )

    assert b'"authorization": "Bearer target"' in result.body
    assert b'"cookie": "session=cookie-value"' in result.body


@pytest.mark.asyncio
async def test_http_fetcher_strips_credentials_on_cross_origin_redirect() -> None:
    async def destination(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "authorization": request.headers.get("Authorization"),
                "cookie": request.headers.get("Cookie"),
            }
        )

    async with _server(destination) as destination_url:

        async def redirect(_request: web.Request) -> web.Response:
            raise web.HTTPFound(destination_url)

        async with _server(redirect) as origin_url:
            fetcher = HttpFetcher(
                validator=_allow_test_target,
                resolver=aiohttp.DefaultResolver(),
                max_download_bytes=10_000,
            )
            result = await fetcher.fetch(
                _request(
                    origin_url,
                    headers={"Authorization": "Bearer target"},
                    cookies={"session": "cookie-value"},
                )
            )

    assert b'"authorization": null' in result.body
    assert b'"cookie": null' in result.body


@pytest.mark.asyncio
async def test_http_fetcher_rejects_oversized_response() -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(body=b"x" * 100)

    async with _server(handler) as base_url:
        fetcher = HttpFetcher(
            validator=_allow_test_target,
            resolver=aiohttp.DefaultResolver(),
            max_download_bytes=50,
        )
        with pytest.raises(ExtractError) as exc_info:
            await fetcher.fetch(_request(base_url))

    assert exc_info.value.code == "response_too_large"
