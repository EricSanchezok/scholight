from __future__ import annotations

from dataclasses import dataclass

import pytest

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.engine import ExtractEngine, ExtractInput, FetchResult


@dataclass
class _StaticFetcher:
    html: str
    calls: int = 0

    async def fetch(self, _request: ExtractInput) -> FetchResult:
        self.calls += 1
        return FetchResult(
            requested_url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            content_type="text/html",
            charset="utf-8",
            body=self.html.encode(),
        )


@dataclass
class _BrowserRenderer:
    html: str
    calls: int = 0

    async def render(self, _request: ExtractInput) -> FetchResult:
        self.calls += 1
        return FetchResult(
            requested_url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            content_type="text/html",
            charset="utf-8",
            body=self.html.encode(),
        )


@pytest.mark.asyncio
async def test_auto_render_falls_back_for_spa_shell() -> None:
    static = _StaticFetcher('<html><body><div id="root"></div><script></script></body></html>')
    browser = _BrowserRenderer(
        "<html><head><title>Rendered</title></head><body><main><p>Rendered body content "
        + "available after JavaScript. " * 20
        + "</p></main></body></html>"
    )
    engine = ExtractEngine(fetcher=static, browser=browser)

    result = await engine.extract(
        ExtractInput(
            url="https://example.com",
            render=RenderMode.AUTO,
            output=ExtractResponseFormat.MAIN_MARKDOWN,
        )
    )

    assert result.rendered is True
    assert browser.calls == 1


@pytest.mark.asyncio
async def test_never_render_uses_static_result() -> None:
    static = _StaticFetcher("<html><body><main><p>Static body</p></main></body></html>")
    browser = _BrowserRenderer("<html><body>Browser body</body></html>")
    engine = ExtractEngine(fetcher=static, browser=browser)

    result = await engine.extract(
        ExtractInput(
            url="https://example.com",
            render=RenderMode.NEVER,
            output=ExtractResponseFormat.FULL_MARKDOWN,
        )
    )

    assert result.rendered is False
    assert browser.calls == 0


@pytest.mark.asyncio
async def test_always_render_skips_static_fetch() -> None:
    static = _StaticFetcher("<html><body>Static</body></html>")
    browser = _BrowserRenderer("<html><body><main>Browser content</main></body></html>")
    engine = ExtractEngine(fetcher=static, browser=browser)

    result = await engine.extract(
        ExtractInput(
            url="https://example.com",
            render=RenderMode.ALWAYS,
            output=ExtractResponseFormat.FULL_MARKDOWN,
        )
    )

    assert (result.rendered, static.calls, browser.calls) == (True, 0, 1)
