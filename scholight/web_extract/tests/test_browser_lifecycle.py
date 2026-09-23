from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from playwright.async_api import Error as PlaywrightError

from scholight.web_extract.browser import PlaywrightBrowserRenderer
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.tests.test_browser import _request

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("stage", ["launch", "context"])
async def test_browser_startup_and_context_failures_are_controlled(stage: str) -> None:
    browser = MagicMock()
    browser.new_context = AsyncMock(side_effect=PlaywrightError("disconnected"))
    renderer = PlaywrightBrowserRenderer(validator=AsyncMock())
    start = AsyncMock(
        side_effect=PlaywrightError("launch failed") if stage == "launch" else None,
        return_value=browser,
    )
    with patch.object(renderer, "_ensure_browser", start), pytest.raises(ExtractError) as error:
        await renderer.render(_request())
    assert error.value.code == "render_failed"
    assert not renderer._semaphore.locked()


async def test_browser_cleanup_preserves_the_original_document_error() -> None:
    context = MagicMock()
    context.close = AsyncMock(side_effect=PlaywrightError("already closed"))
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    original = ExtractError(
        code="private_target", message="private target", status_code=403, retryable=False
    )
    renderer = PlaywrightBrowserRenderer(validator=AsyncMock())
    with (
        patch.object(renderer, "_ensure_browser", AsyncMock(return_value=browser)),
        patch.object(renderer, "_configure_context", AsyncMock(side_effect=original)),
        pytest.raises(ExtractError) as error,
    ):
        await renderer.render(_request())
    assert error.value is original


async def test_route_close_race_is_consumed_by_the_callback() -> None:
    context = MagicMock()
    context.route = AsyncMock()
    context.route_web_socket = AsyncMock()
    renderer = PlaywrightBrowserRenderer(validator=AsyncMock())
    await renderer._configure_context(context, _request())
    handler = context.route.call_args.args[1]
    route = MagicMock()
    route.request.url = "https://example.com"
    route.request.method = "GET"
    route.request.all_headers = AsyncMock(return_value={})
    route.continue_ = AsyncMock(side_effect=PlaywrightError("target closed"))
    await handler(route)
