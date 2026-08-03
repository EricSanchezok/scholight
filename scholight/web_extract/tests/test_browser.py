from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from aiohttp import web

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.browser import PlaywrightBrowserRenderer, _cookie_map
from scholight.web_extract.engine import ExtractInput


def _request(**overrides: object) -> ExtractInput:
    values: dict[str, object] = {
        "url": "https://example.com",
        "render": RenderMode.ALWAYS,
        "output": ExtractResponseFormat.MAIN_MARKDOWN,
    }
    values.update(overrides)
    return ExtractInput(**values)  # type: ignore[arg-type]


@asynccontextmanager
async def _server(handler: web.RequestHandler, *, hostname: str) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server is not None else []
    port = sockets[0].getsockname()[1]
    try:
        yield f"http://{hostname}:{port}"
    finally:
        await runner.cleanup()


async def _allow_test_target(_url: str) -> None:
    return None


def test_browser_treats_every_caller_header_as_origin_scoped() -> None:
    request = _request(
        headers={
            "Accept-Language": "zh-CN",
            "Authorization": "Bearer target",
            "X-Api-Key": "target-api-key",
        }
    )

    assert request.headers == {
        "Accept-Language": "zh-CN",
        "Authorization": "Bearer target",
        "X-Api-Key": "target-api-key",
    }


def test_browser_accepts_cookie_header_as_stateless_cookie_input() -> None:
    request = _request(headers={"Cookie": "session=abc; theme=dark"})

    assert _cookie_map(request) == {"session": "abc", "theme": "dark"}


@pytest.mark.browser_integration
@pytest.mark.asyncio
async def test_browser_enforces_origin_and_method_boundaries() -> None:
    destination_requests: list[tuple[str, str | None, str | None, str | None]] = []
    origin_paths: list[str] = []

    async def destination(request: web.Request) -> web.Response:
        destination_requests.append(
            (
                request.method,
                request.headers.get("Authorization"),
                request.headers.get("X-Api-Key"),
                request.headers.get("Cookie"),
            )
        )
        return web.json_response(
            {"ok": True},
            headers={"Access-Control-Allow-Origin": "*"},
        )

    async with _server(destination, hostname="localhost") as destination_url:

        async def origin(request: web.Request) -> web.Response:
            origin_paths.append(request.path)
            if request.path == "/same-origin":
                return web.json_response(
                    {
                        "method": request.method,
                        "authorization": request.headers.get("Authorization"),
                        "api_key": request.headers.get("X-Api-Key"),
                        "cookie": request.headers.get("Cookie"),
                    }
                )
            if request.path == "/popup":
                return web.Response(text="popup")
            if request.path == "/sw.js":
                return web.Response(
                    text="self.addEventListener('fetch', () => {});",
                    content_type="application/javascript",
                )
            html = f"""
                <html><body><main id="result">pending</main><script>
                (async () => {{
                  const same = await fetch('/same-origin', {{method: 'POST'}}).then(r => r.json());
                  const crossGet = await fetch('{destination_url}/cross').then(() => 'ok');
                  const blocked = [];
                  for (const [url, method] of [
                    ['/same-origin', 'DELETE'],
                    ['{destination_url}/cross', 'POST'],
                  ]) {{
                    try {{ await fetch(url, {{method}}); blocked.push('failed'); }}
                    catch (_error) {{ blocked.push('blocked'); }}
                  }}
                  try {{ await navigator.serviceWorker.register('/sw.js'); }}
                  catch (_error) {{}}
                  const serviceWorker = (await navigator.serviceWorker.getRegistrations()).length === 0
                    ? 'blocked' : 'failed';
                  let webSocket = 'pending';
                  const socket = new WebSocket('ws://127.0.0.1:9/socket');
                  socket.onclose = () => {{ webSocket = 'blocked'; }};
                  const popup = window.open('/popup');
                  await new Promise(resolve => setTimeout(resolve, 250));
                  document.querySelector('#result').textContent = JSON.stringify({{
                    same, crossGet, blocked, serviceWorker, webSocket,
                    popupClosed: popup === null || popup.closed,
                  }});
                }})().catch(error => {{
                  document.querySelector('#result').textContent = `error:${{error}}`;
                }});
                </script></body></html>
            """
            return web.Response(text=html, content_type="text/html")

        async with _server(origin, hostname="127.0.0.1") as origin_url:
            renderer = PlaywrightBrowserRenderer(
                validator=_allow_test_target,
                timeout_seconds=10,
            )
            try:
                result = await renderer.render(
                    _request(
                        url=origin_url,
                        headers={
                            "Authorization": "Bearer target",
                            "X-Api-Key": "target-api-key",
                        },
                        cookies={"session": "cookie-value"},
                    )
                )
            finally:
                await renderer.close()

    assert '"authorization":"Bearer target"' in result.body.decode()
    assert '"api_key":"target-api-key"' in result.body.decode()
    assert '"cookie":"session=cookie-value"' in result.body.decode()
    assert '"blocked":["blocked","blocked"]' in result.body.decode()
    assert '"serviceWorker":"blocked"' in result.body.decode()
    assert '"webSocket":"blocked"' in result.body.decode()
    assert '"popupClosed":true' in result.body.decode()
    assert destination_requests == [("GET", None, None, None)]
    assert "/sw.js" not in origin_paths


@pytest.mark.asyncio
async def test_browser_restarts_a_disconnected_instance() -> None:
    renderer = PlaywrightBrowserRenderer(validator=_allow_test_target)

    class _DisconnectedBrowser:
        def is_connected(self) -> bool:
            return False

        async def close(self) -> None:
            return None

    renderer._browser = _DisconnectedBrowser()  # type: ignore[assignment]
    replacement = object()

    class _Chromium:
        async def launch(self, **_kwargs: object) -> object:
            return replacement

    class _Playwright:
        chromium = _Chromium()

    renderer._playwright = _Playwright()  # type: ignore[assignment]

    assert await renderer._ensure_browser() is replacement
