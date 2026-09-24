from __future__ import annotations

from datetime import UTC, datetime

import aiohttp
import pytest
from aiohttp import web

from scholight.web_extract.errors import ExtractError
from scholight.web_extract.fetcher import HttpFetcher
from scholight.web_extract.http_retry import retry_after
from scholight.web_extract.tests.test_fetcher import _allow_test_target, _request, _server

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("headers", [{}, {"Authorization": "fixture"}])
async def test_only_public_static_get_retries_a_transient_status_once(headers) -> None:
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return web.Response(status=503 if calls == 1 else 200, text="evidence")

    async with _server(handler) as base:
        fetcher = HttpFetcher(
            validator=_allow_test_target, resolver=aiohttp.DefaultResolver(), retry_enabled=True
        )
        if headers:
            with pytest.raises(ExtractError):
                await fetcher.fetch(_request(base, headers=headers))
            assert calls == 1
        else:
            assert (await fetcher.fetch(_request(base))).body == b"evidence"
            assert calls == 2


@pytest.mark.parametrize("retry_enabled,expected", [(False, 1), (True, 2)])
async def test_transport_cannot_add_hidden_replays_on_top_of_the_retry_budget(
    retry_enabled, expected
) -> None:
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        request.transport.abort()
        return web.Response(text="unreachable")

    async with _server(handler) as base:
        fetcher = HttpFetcher(
            validator=_allow_test_target,
            resolver=aiohttp.DefaultResolver(),
            retry_enabled=retry_enabled,
        )
        with pytest.raises(ExtractError):
            await fetcher.fetch(_request(base))
    assert calls == expected


async def test_retry_after_longer_than_remaining_budget_is_not_ignored() -> None:
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return web.Response(status=429, headers={"Retry-After": "120"})

    async with _server(handler) as base:
        fetcher = HttpFetcher(
            validator=_allow_test_target,
            resolver=aiohttp.DefaultResolver(),
            timeout_seconds=1,
            retry_enabled=True,
        )
        with pytest.raises(ExtractError):
            await fetcher.fetch(_request(base))
    assert calls == 1


@pytest.mark.parametrize(
    "value,expected",
    [
        ("17", 17),
        ("Wed, 23 Sep 2026 10:01:00 GMT", 60),
        ("Wed, 23 Sep 2026 09:59:00 GMT", 0),
        ("-1", None),
        ("1.5", None),
        ("invalid", None),
        (None, None),
        ("9" * 129, None),
    ],
)
async def test_retry_after_accepts_http_dates_and_nonnegative_integer_seconds(
    value, expected
) -> None:
    assert retry_after(value, now=datetime(2026, 9, 23, 10, tzinfo=UTC)) == expected


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 501, 403, 408])
async def test_retry_status_allowlist_and_attempt_limit(status) -> None:
    calls = 0

    async def handler(_request):
        nonlocal calls
        calls += 1
        return web.Response(status=status)

    async with _server(handler) as base:
        fetcher = HttpFetcher(
            validator=_allow_test_target,
            resolver=aiohttp.DefaultResolver(),
            retry_enabled=True,
        )
        with pytest.raises(ExtractError):
            await fetcher.fetch(_request(base))
    assert calls == (2 if status in {429, 500, 502, 503, 504} else 1)
