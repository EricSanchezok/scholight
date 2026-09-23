from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from scholight.web_extract.errors import ExtractError
from scholight.web_extract.reservations import MemoryBudget, MemoryModel, StageCost


def _model() -> MemoryModel:
    return MemoryModel(
        download=StageCost(10, 1),
        html=StageCost(20, 2),
        pdf=StageCost(30, 3),
        text=StageCost(5, 1),
        browser=StageCost(50, 0),
    )


def test_phase_transition_replaces_the_same_reservation() -> None:
    budget = MemoryBudget(lambda: 100, lambda: None, model=_model(), high=200)
    lease = budget.lease()
    lease.transfer("download", size=10)
    assert budget.reserved_bytes == 20
    lease.transfer("parse", size=10, mime="text/html")
    assert budget.reserved_bytes == 40
    lease.close()
    lease.close()
    assert budget.reserved_bytes == 0


def test_competing_work_and_fresh_working_set_limit_admission() -> None:
    working = 100
    budget = MemoryBudget(lambda: working, lambda: None, model=_model(), high=200)
    first, second = budget.lease(), budget.lease()
    first.transfer("browser")
    with pytest.raises(ExtractError):
        second.transfer("parse", size=15, mime="application/pdf")
    assert budget.reserved_bytes == 50
    working = 151
    with pytest.raises(ExtractError):
        first.transfer("browser")
    first.close()
    second.close()
    assert budget.reserved_bytes == 0


def test_failed_phase_growth_preserves_prior_ownership_for_cleanup() -> None:
    budget = MemoryBudget(lambda: 100, lambda: None, model=_model(), high=150)
    lease = budget.lease()
    lease.transfer("download", size=10)
    with pytest.raises(ExtractError):
        lease.transfer("parse", size=100, mime="text/html")
    assert budget.reserved_bytes == 20
    lease.close()
    assert budget.reserved_bytes == 0


def test_startup_envelope_adds_to_job_and_releases_after_readiness() -> None:
    model = MemoryModel(download=StageCost(10, 1), parser_startup=80, browser_startup=90)
    budget = MemoryBudget(lambda: 100, lambda: None, model=model, high=200)
    lease = budget.lease()
    lease.transfer("download", size=10)
    with budget.startup("parser"):
        assert budget.reserved_bytes == 100
    assert budget.reserved_bytes == 20
    with pytest.raises(ExtractError):
        with budget.startup("browser"):
            pytest.fail("A cold browser exceeded the combined memory budget")
    assert budget.reserved_bytes == 20
    lease.close()


@pytest.mark.asyncio
async def test_cancelled_worker_queue_has_no_parse_or_scratch_reservation(tmp_path) -> None:
    from scholight.models.web_extract import ExtractResponseFormat, RenderMode
    from scholight.web_extract.engine import ExtractInput, FetchResult
    from scholight.web_extract.isolated import IsolatedParser
    from scholight.web_extract.spool import Spool
    from scholight.web_extract.worker_supervisor import WorkerSupervisor

    budget = MemoryBudget(lambda: 100, lambda: None, model=_model(), high=1000)
    worker = WorkerSupervisor("parser", queueing=True)
    worker._start = AsyncMock()
    await worker._gate.acquire()
    spool = Spool(tmp_path, max_bytes=2000)
    spool.start()
    parser = IsolatedParser(worker, spool, max_output_bytes=1000)
    lease = budget.lease()
    lease.transfer("download", size=5)
    with spool.allocate(5) as body:
        body.write(b"hello")
        fetched = FetchResult(
            "https://example.com",
            "https://example.com",
            200,
            "text/plain",
            "utf-8",
            spool_file=body,
            reservation=lease,
        )
        task = asyncio.create_task(
            parser.parse(
                fetched,
                ExtractInput("https://example.com", RenderMode.NEVER, ExtractResponseFormat.TEXT),
                rendered=False,
            )
        )
        await asyncio.sleep(0)
        assert budget.reserved_bytes == 15 and spool.reserved_bytes == 5
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        fetched.close()
    worker._gate.release()
    spool.close()
    assert budget.reserved_bytes == 0


@pytest.mark.asyncio
async def test_stream_growth_rejection_releases_file_memory_and_download_permit(tmp_path) -> None:
    import aiohttp
    from aiohttp import web

    from scholight.web_extract.fetcher import HttpFetcher
    from scholight.web_extract.spool import Spool
    from scholight.web_extract.tests.test_fetcher import _allow_test_target, _request, _server

    async def handler(_request):
        return web.Response(body=b"x" * 100)

    budget = MemoryBudget(lambda: 100, lambda: None, model=_model(), high=150)
    spool = Spool(tmp_path, max_bytes=1000)
    spool.start()
    fetcher = HttpFetcher(
        validator=_allow_test_target,
        resolver=aiohttp.DefaultResolver(),
        max_download_bytes=500,
        spool=spool,
        memory=budget,
    )
    try:
        async with _server(handler) as base:
            with pytest.raises(ExtractError) as error:
                await fetcher.fetch(_request(base))
            assert error.value.code == "extract_capacity_exceeded"
        assert budget.reserved_bytes == 0 and spool.reserved_bytes == 0
        assert fetcher._gate.active == 0
    finally:
        await fetcher.close()
        spool.close()
