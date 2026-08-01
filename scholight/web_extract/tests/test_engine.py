from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import patch

import pytest

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.engine import (
    ExtractEngine,
    ExtractInput,
    FetchResult,
)
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.extractors import ExtractedContent


@dataclass
class _StaticFetcher:
    html: str
    content_type: str = "text/html"
    calls: int = 0

    async def fetch(self, _request: ExtractInput) -> FetchResult:
        self.calls += 1
        return FetchResult(
            requested_url="https://example.com",
            final_url="https://example.com",
            status_code=200,
            content_type=self.content_type,
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "body", "expected"),
    [
        ("application/json", '{"message":"你好"}', '"message": "你好"'),
        ("application/xml", "<root><value>42</value></root>", "<value>42</value>"),
        ("text/markdown", "# Heading\n\nBody", "# Heading"),
    ],
)
async def test_textual_formats_are_normalized(
    content_type: str,
    body: str,
    expected: str,
) -> None:
    static = _StaticFetcher(body, content_type=content_type)
    engine = ExtractEngine(fetcher=static, browser=_BrowserRenderer("unused"))

    result = await engine.extract(
        ExtractInput(
            url="https://example.com/data",
            render=RenderMode.NEVER,
            output=ExtractResponseFormat.TEXT,
        )
    )

    assert expected in result.content


@pytest.mark.asyncio
async def test_pdf_is_routed_to_pdf_extractor() -> None:
    static = _StaticFetcher("%PDF-fixture", content_type="application/pdf")
    engine = ExtractEngine(fetcher=static, browser=_BrowserRenderer("unused"))
    extracted = ExtractedContent(
        content="# Paper",
        title="Paper",
        author="Author",
        published_at=None,
        extractor="pymupdf4llm",
    )

    with patch("scholight.web_extract.engine._pdf", return_value=extracted) as pdf:
        result = await engine.extract(
            ExtractInput(
                url="https://example.com/paper.pdf",
                render=RenderMode.NEVER,
                output=ExtractResponseFormat.MAIN_MARKDOWN,
            )
        )

    pdf.assert_called_once_with(b"%PDF-fixture")
    assert (result.title, result.extractor) == ("Paper", "pymupdf4llm")


@pytest.mark.asyncio
async def test_unsupported_binary_content_type_has_stable_error_code() -> None:
    static = _StaticFetcher("binary", content_type="application/octet-stream")
    engine = ExtractEngine(fetcher=static, browser=_BrowserRenderer("unused"))

    with pytest.raises(ExtractError) as exc_info:
        await engine.extract(
            ExtractInput(
                url="https://example.com/file.bin",
                render=RenderMode.NEVER,
                output=ExtractResponseFormat.TEXT,
            )
        )

    assert exc_info.value.code == "unsupported_content_type"
