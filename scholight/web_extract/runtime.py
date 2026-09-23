"""Runtime assembly for the internal Extract sidecar."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from scholight.config import settings, validate_extract_runtime_settings
from scholight.web_extract.engine import ExtractEngine
from scholight.web_extract.fetcher import HttpFetcher
from scholight.web_extract.isolated import IsolatedBrowser, IsolatedParser
from scholight.web_extract.process_family import enable_subreaping
from scholight.web_extract.service import create_extract_service
from scholight.web_extract.spool import Spool
from scholight.web_extract.worker_supervisor import WorkerSupervisor


def build_extract_app() -> FastAPI:
    validate_extract_runtime_settings()
    spool = Spool(Path(settings.data_root) / "extract-spool")
    parser_worker = WorkerSupervisor("parser")
    browser_worker = WorkerSupervisor("browser")
    browser = IsolatedBrowser(
        browser_worker,
        spool,
        max_content_bytes=settings.extract_max_download_bytes,
    )
    engine = ExtractEngine(
        fetcher=HttpFetcher(
            max_download_bytes=settings.extract_max_download_bytes,
            timeout_seconds=settings.extract_fetch_timeout_seconds,
            concurrency=settings.extract_static_concurrency,
            spool=spool,
        ),
        browser=browser,
        parser=IsolatedParser(
            parser_worker,
            spool,
            max_output_bytes=settings.extract_max_download_bytes,
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        enable_subreaping()
        spool.start()
        try:
            await parser_worker.warmup()
            await browser_worker.warmup()
            yield
        finally:
            try:
                await browser_worker.close()
                await parser_worker.close()
            finally:
                spool.close()

    return create_extract_service(
        engine=engine,
        internal_token=settings.extract_internal_token,
        cache_ttl_seconds=settings.extract_cache_ttl_seconds,
        cache_max_bytes=settings.extract_cache_max_bytes,
        lifespan=lifespan,
    )


__all__ = ["build_extract_app"]
