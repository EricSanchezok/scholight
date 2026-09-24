from __future__ import annotations

import asyncio

import httpx
import pytest

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.engine import ExtractEngine, ExtractInput
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.service import create_extract_service
from scholight.web_extract.singleflight import Singleflight
from scholight.web_extract.tests.test_engine import _BrowserRenderer, _StaticFetcher

pytestmark = pytest.mark.asyncio


async def test_eight_waiters_share_one_operation_and_cancel_independently() -> None:
    group = Singleflight[int]()
    started, finish = asyncio.Event(), asyncio.Event()
    calls = 0

    async def work(_work_id):
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        return 42

    tasks = [asyncio.create_task(group.do("same", work)) for _ in range(8)]
    await started.wait()
    tasks[0].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[0]
    assert group.active_keys == 1 and calls == 1
    finish.set()
    assert await asyncio.gather(*tasks[1:]) == [42] * 7
    assert group.active_keys == 0


async def test_all_waiters_leaving_stops_and_awaits_owned_work() -> None:
    group = Singleflight[int]()
    started, stopped = asyncio.Event(), asyncio.Event()

    async def work(_work_id):
        started.set()
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            stopped.set()

    task = asyncio.create_task(group.do("same", work))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set() and group.active_keys == 0


async def test_inflight_keys_and_waiter_counts_are_bounded() -> None:
    group = Singleflight[int](max_keys=2, max_waiters=2)
    started, finish = asyncio.Event(), asyncio.Event()

    async def work(_work_id):
        started.set()
        await finish.wait()
        return 1

    first = asyncio.create_task(group.do("one", work))
    await started.wait()
    second = asyncio.create_task(group.do("one", work))
    third = asyncio.create_task(group.do("two", work))
    await asyncio.sleep(0)
    try:
        with pytest.raises(ExtractError):
            await group.do("one", work)
        with pytest.raises(ExtractError):
            await group.do("three", work)
    finally:
        finish.set()
        await asyncio.gather(first, second, third)
    assert group.active_keys == 0


@pytest.mark.parametrize("credentials", ["none", "headers", "cookies"])
async def test_engine_merges_only_identical_uncredentialed_static_work(credentials: str) -> None:
    fetcher = _StaticFetcher(
        "<html><body><article><p>Static evidence. "
        + "Complete body content. " * 20
        + "</p></article></body></html>"
    )
    browser = _BrowserRenderer("unused")
    engine = ExtractEngine(fetcher=fetcher, browser=browser, singleflight=True)
    request = ExtractInput(
        "https://example.com",
        RenderMode.AUTO,
        ExtractResponseFormat.MAIN_MARKDOWN,
        headers={"Authorization": "fixture"} if credentials == "headers" else {},
        cookies={"session": "fixture"} if credentials == "cookies" else {},
    )
    results = await asyncio.gather(*(engine.extract(request) for _ in range(8)))
    assert fetcher.calls == (1 if credentials == "none" else 8)
    assert len({r.content_hash for r in results}) == 1
    assert browser.calls == 0


async def test_shared_static_spa_detection_does_not_merge_browser_operations() -> None:
    fetcher = _StaticFetcher('<div id="root">Enable JavaScript</div><script></script>')
    browser = _BrowserRenderer(
        "<html><body><article><p>Rendered evidence. "
        + "Complete body content. " * 20
        + "</p></article></body></html>"
    )
    engine = ExtractEngine(fetcher=fetcher, browser=browser, singleflight=True)
    request = ExtractInput(
        "https://example.com", RenderMode.AUTO, ExtractResponseFormat.MAIN_MARKDOWN
    )
    await asyncio.gather(*(engine.extract(request) for _ in range(8)))
    assert fetcher.calls == 1 and browser.calls == 8


async def test_first_caller_deadline_does_not_expire_another_internal_request() -> None:
    started, finish = asyncio.Event(), asyncio.Event()

    class SlowFetcher(_StaticFetcher):
        async def fetch(self, request):
            started.set()
            await finish.wait()
            return await super().fetch(request)

    fetcher = SlowFetcher("<html><body><p>Complete static evidence.</p></body></html>")
    engine = ExtractEngine(fetcher=fetcher, browser=_BrowserRenderer("unused"), singleflight=True)
    app = create_extract_service(engine=engine, internal_token="fixture")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://extract"
    ) as client:
        headers = {"X-Scholight-Internal-Token": "fixture"}
        first = asyncio.create_task(
            client.post(
                "/v1/extract",
                json={"url": "https://example.com"},
                headers={**headers, "X-Scholight-Budget-Ms": "2100"},
            )
        )
        await started.wait()
        second = asyncio.create_task(
            client.post(
                "/v1/extract",
                json={"url": "https://example.com"},
                headers=headers,
            )
        )
        try:
            assert (await first).status_code == 504
            assert not second.done()
            finish.set()
            assert (await second).status_code == 200
            assert fetcher.calls == 1
        finally:
            finish.set()
            await asyncio.gather(first, second, return_exceptions=True)
            await engine.close()
