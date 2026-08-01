"""Isolated Playwright renderer for JavaScript-dependent pages."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Playwright,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)

from scholight.web_extract.engine import ExtractInput, FetchResult
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.policy import validate_public_target

_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})


def _origin(url: str) -> tuple[str, str | None, int]:
    parsed = urlsplit(url)
    return (
        parsed.scheme.lower(),
        parsed.hostname,
        parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
    )


def _split_headers(headers: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    regular: dict[str, str] = {}
    sensitive: dict[str, str] = {}
    for name, value in headers.items():
        target = sensitive if name.lower() in _SENSITIVE_HEADERS else regular
        target[name] = value
    return regular, sensitive


def _cookie_map(request: ExtractInput) -> dict[str, str]:
    if request.cookies:
        return request.cookies
    raw = next((value for name, value in request.headers.items() if name.lower() == "cookie"), None)
    if raw is None:
        return {}
    parsed = SimpleCookie()
    parsed.load(raw)
    return {name: morsel.value for name, morsel in parsed.items()}


class PlaywrightBrowserRenderer:
    def __init__(
        self,
        *,
        timeout_seconds: float = 45.0,
        concurrency: int = 2,
        max_content_bytes: int = 50_000_000,
    ) -> None:
        self._timeout_ms = int(timeout_seconds * 1000)
        self._semaphore = asyncio.Semaphore(concurrency)
        self._max_content_bytes = max_content_bytes
        self._startup_lock = asyncio.Lock()
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def _ensure_browser(self) -> Browser:
        if self._browser is not None:
            return self._browser
        async with self._startup_lock:
            if self._browser is None:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=True,
                    args=["--disable-dev-shm-usage", "--no-sandbox"],
                )
        return self._browser

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def warmup(self) -> None:
        """Start Chromium so sidecar readiness proves the renderer is installed."""
        await self._ensure_browser()

    async def _configure_context(
        self,
        context: BrowserContext,
        request: ExtractInput,
    ) -> None:
        regular_headers, sensitive_headers = _split_headers(request.headers)
        sensitive_headers = {
            name: value for name, value in sensitive_headers.items() if name.lower() != "cookie"
        }
        regular_headers.setdefault("User-Agent", "Scholight-Web-Extract/1.0")
        await context.set_extra_http_headers(regular_headers)
        cookies = _cookie_map(request)
        if cookies:
            await context.add_cookies(
                [
                    {"name": name, "value": value, "url": request.url}
                    for name, value in cookies.items()
                ]
            )
        initial_origin = _origin(request.url)

        async def enforce_policy(route: Route) -> None:
            target_url = route.request.url
            parsed = urlsplit(target_url)
            if parsed.scheme in {"about", "blob", "data"}:
                await route.continue_()
                return
            if parsed.scheme not in {"http", "https"}:
                await route.abort("blockedbyclient")
                return
            try:
                await validate_public_target(target_url)
            except ExtractError:
                await route.abort("blockedbyclient")
                return
            if _origin(target_url) == initial_origin and sensitive_headers:
                forwarded = await route.request.all_headers()
                forwarded.update(sensitive_headers)
                await route.continue_(headers=forwarded)
                return
            await route.continue_()

        await context.route("**/*", enforce_policy)

    async def render(self, request: ExtractInput) -> FetchResult:
        if self._semaphore.locked():
            raise ExtractError(
                code="extract_capacity_exceeded",
                message="Browser extraction capacity is temporarily exhausted.",
                status_code=503,
                retryable=True,
            )
        await self._semaphore.acquire()
        try:
            await validate_public_target(request.url)
            browser = await self._ensure_browser()
            context = await browser.new_context(ignore_https_errors=False)
            try:
                await self._configure_context(context, request)
                page = await context.new_page()
                response = await page.goto(
                    request.url,
                    wait_until="domcontentloaded",
                    timeout=self._timeout_ms,
                )
                if response is None:
                    raise ExtractError(
                        code="render_failed",
                        message="Browser navigation completed without a response.",
                        status_code=502,
                        retryable=True,
                    )
                if response.status >= 400:
                    raise ExtractError(
                        code="upstream_http_error",
                        message=f"Target returned HTTP {response.status}.",
                        status_code=502,
                        retryable=response.status == 429 or response.status >= 500,
                    )
                with suppress(PlaywrightTimeoutError):
                    await page.wait_for_load_state(
                        "networkidle", timeout=min(2500, self._timeout_ms)
                    )
                await page.evaluate(
                    """
                    () => new Promise((resolve) => {
                      const quietMs = 250;
                      const maximumMs = 1000;
                      let quietTimer;
                      let maximumTimer;
                      const observer = new MutationObserver(() => schedule());
                      const finish = () => {
                        observer.disconnect();
                        clearTimeout(quietTimer);
                        clearTimeout(maximumTimer);
                        resolve();
                      };
                      const schedule = () => {
                        clearTimeout(quietTimer);
                        quietTimer = setTimeout(finish, quietMs);
                      };
                      observer.observe(document.documentElement, {
                        childList: true,
                        subtree: true,
                        attributes: true,
                        characterData: true,
                      });
                      maximumTimer = setTimeout(finish, maximumMs);
                      schedule();
                    })
                    """
                )
                final_url = page.url
                await validate_public_target(final_url)
                html = await page.content()
                encoded_html = html.encode("utf-8")
                if len(encoded_html) > self._max_content_bytes:
                    raise ExtractError(
                        code="response_too_large",
                        message="Rendered page exceeds the download limit.",
                        status_code=413,
                        retryable=False,
                    )
                return FetchResult(
                    requested_url=request.url,
                    final_url=final_url,
                    status_code=response.status,
                    content_type=response.headers.get("content-type", "text/html; charset=utf-8"),
                    charset="utf-8",
                    body=encoded_html,
                )
            except ExtractError:
                raise
            except PlaywrightTimeoutError as exc:
                raise ExtractError(
                    code="render_timeout",
                    message="Browser rendering exceeded the timeout.",
                    status_code=504,
                    retryable=True,
                ) from exc
            except PlaywrightError as exc:
                raise ExtractError(
                    code="render_failed",
                    message="Browser rendering failed.",
                    status_code=502,
                    retryable=True,
                ) from exc
            finally:
                await context.close()
        finally:
            self._semaphore.release()


__all__ = ["PlaywrightBrowserRenderer"]
