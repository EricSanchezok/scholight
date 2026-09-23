"""Runtime assembly for the internal Extract sidecar."""

from __future__ import annotations

import asyncio
import resource
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from scholight.config import settings, validate_extract_runtime_settings
from scholight.logging.emf import emit_emf
from scholight.web_extract.engine import ExtractEngine
from scholight.web_extract.fetcher import HttpFetcher
from scholight.web_extract.isolated import IsolatedBrowser, IsolatedParser
from scholight.web_extract.memory import MemoryGuard, MemorySample, read_cgroup
from scholight.web_extract.process_family import enable_subreaping, family_rss
from scholight.web_extract.service import create_extract_service
from scholight.web_extract.spool import Spool
from scholight.web_extract.worker_supervisor import WorkerSupervisor


def build_extract_app() -> FastAPI:
    validate_extract_runtime_settings()
    spool = Spool(Path(settings.data_root) / "extract-spool")
    parser_worker = WorkerSupervisor(
        "parser",
        queueing=settings.extract_queueing,
        admit=lambda: memory.admit(),
    )
    browser_worker = WorkerSupervisor(
        "browser",
        queueing=settings.extract_queueing,
        admit=lambda: memory.admit(),
    )

    async def reclaim() -> None:
        app.state.extract_cache.clear()
        await asyncio.gather(browser_worker.close(), parser_worker.close(), fetcher.close())

    def sample_memory() -> MemorySample:
        parser_rss = family_rss(parser_worker.pid)
        browser_rss = family_rss(browser_worker.pid)
        emit_emf(
            service="extract",
            metrics={
                "ParserRSS": (parser_rss, "Bytes"),
                "BrowserRSS": (browser_rss, "Bytes"),
                "ParserActive": (int(parser_worker.busy), "Count"),
                "BrowserActive": (int(browser_worker.busy), "Count"),
                "ParserStarts": (parser_worker.restarts, "Count"),
                "BrowserStarts": (browser_worker.restarts, "Count"),
                "ScratchReservedBytes": (spool.reserved_bytes, "Bytes"),
            },
        )
        if sys.platform == "linux":
            return read_cgroup()
        # Local macOS development has no container cgroup; use a conservative RSS bound.
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss + parser_rss + browser_rss
        return MemorySample(working_set=rss, anon=rss, file=0)

    memory = MemoryGuard(sample_memory, reclaim)
    browser = IsolatedBrowser(
        browser_worker,
        spool,
        max_content_bytes=settings.extract_max_download_bytes,
    )
    fetcher = HttpFetcher(
        max_download_bytes=settings.extract_max_download_bytes,
        timeout_seconds=settings.extract_fetch_timeout_seconds,
        concurrency=settings.extract_static_concurrency,
        spool=spool,
        queueing=settings.extract_queueing,
        admit=memory.admit,
        reuse_connections=settings.extract_connection_reuse,
        retry_enabled=True,
    )
    engine = ExtractEngine(
        fetcher=fetcher,
        browser=browser,
        admit=memory.admit,
        singleflight=settings.extract_singleflight,
        parser=IsolatedParser(
            parser_worker,
            spool,
            max_output_bytes=settings.extract_max_download_bytes,
            reuse_quality=settings.extract_parse_reuse,
        ),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        enable_subreaping()
        spool.start()
        try:
            await parser_worker.warmup()
            await browser_worker.warmup()
            async with memory.monitor():
                yield
        finally:
            try:
                try:
                    await engine.close()
                finally:
                    await asyncio.gather(
                        browser_worker.close(), parser_worker.close(), fetcher.close()
                    )
            finally:
                spool.close()

    app = create_extract_service(
        engine=engine,
        internal_token=settings.extract_internal_token,
        cache_ttl_seconds=settings.extract_cache_ttl_seconds,
        cache_max_bytes=settings.extract_cache_max_bytes,
        lifespan=lifespan,
        admit=memory.admit,
    )
    return app


__all__ = ["build_extract_app"]
