"""Runtime assembly for the internal Extract sidecar."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from scholight.config import settings, validate_extract_runtime_settings
from scholight.web_extract.browser import PlaywrightBrowserRenderer
from scholight.web_extract.engine import ExtractEngine
from scholight.web_extract.fetcher import HttpFetcher
from scholight.web_extract.service import create_extract_service


def build_extract_app() -> FastAPI:
    validate_extract_runtime_settings()
    browser = PlaywrightBrowserRenderer(
        timeout_seconds=settings.extract_render_timeout_seconds,
        concurrency=settings.extract_browser_concurrency,
        max_content_bytes=settings.extract_max_download_bytes,
    )
    engine = ExtractEngine(
        fetcher=HttpFetcher(
            max_download_bytes=settings.extract_max_download_bytes,
            timeout_seconds=settings.extract_fetch_timeout_seconds,
            concurrency=settings.extract_static_concurrency,
        ),
        browser=browser,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            await browser.warmup()
            yield
        finally:
            await browser.close()

    return create_extract_service(
        engine=engine,
        internal_token=settings.extract_internal_token,
        cache_ttl_seconds=settings.extract_cache_ttl_seconds,
        cache_max_bytes=settings.extract_cache_max_bytes,
        lifespan=lifespan,
    )


__all__ = ["build_extract_app"]
