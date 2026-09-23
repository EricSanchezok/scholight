from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.engine import ExtractInput, FetchResult
from scholight.web_extract.isolated import IsolatedParser
from scholight.web_extract.spool import Spool
from scholight.web_extract.worker_supervisor import WorkerSupervisor


@pytest.mark.asyncio
async def test_parser_exchanges_files_and_releases_result_storage(tmp_path: Path) -> None:
    worker = WorkerSupervisor("parser")
    spool = Spool(tmp_path, max_bytes=2_000_000)
    spool.start()
    parser = IsolatedParser(worker, spool, max_output_bytes=1_000_000)
    try:
        with spool.allocate(1000) as body:
            body.write(b"<html><article><p>Isolated parser evidence.</p></article></html>")
            fetched = FetchResult(
                requested_url="https://example.com",
                final_url="https://example.com",
                status_code=200,
                content_type="text/html",
                charset="utf-8",
                spool_file=body,
            )
            result = await parser.parse(
                fetched,
                ExtractInput(
                    url="https://example.com",
                    render=RenderMode.NEVER,
                    output=ExtractResponseFormat.FULL_MARKDOWN,
                ),
                rendered=False,
            )
            assert result.extracted is not None
            assert "Isolated parser evidence." in result.extracted.content
            assert spool.reserved_bytes == 1000
        assert spool.reserved_bytes == 0
    finally:
        await worker.close()
        spool.close()


@pytest.mark.asyncio
async def test_parser_freezes_only_startup_objects_and_collects_request_cycles(
    tmp_path: Path,
) -> None:
    # Run the actual worker loop in a child: changing GC generations in pytest's
    # interpreter would affect unrelated tests. The injected parser creates a
    # reference cycle so its lifetime is observable without timing assertions.
    program = """
import asyncio, gc, sys, weakref
from contextlib import redirect_stdout
from scholight.web_extract import worker
from scholight.web_extract.engine import ParsedContent
from scholight.web_extract.extractors import ExtractedContent
class RequestCycle: pass
previous = None
startup_count = None
def parse(*args, **kwargs):
    global previous, startup_count
    assert gc.get_freeze_count() > 0, 'Startup graph was not isolated from job GC'
    if startup_count is None:
        startup_count = gc.get_freeze_count()
    assert gc.get_freeze_count() <= startup_count, 'A request was frozen permanently'
    assert previous is None or previous() is None, 'Request cycle survived cache cleanup'
    item = RequestCycle()
    item.cycle = item
    previous = weakref.ref(item)
    return ParsedContent(extracted=ExtractedContent(
        content='collectible', title=None, author=None, published_at=None, extractor='text'))
worker.parse_document = parse
channel = sys.stdout
with redirect_stdout(sys.stderr):
    asyncio.run(worker._main('parser', channel))
"""
    worker = WorkerSupervisor("parser", command=(sys.executable, "-u", "-c", program))
    spool = Spool(tmp_path)
    spool.start()
    parser = IsolatedParser(worker, spool, max_output_bytes=10000)
    try:
        with spool.allocate(100) as body:
            body.write(b"Request data must never enter the permanent generation.")
            for _ in range(3):
                result = await parser.parse(
                    FetchResult(
                        "https://example.org",
                        "https://example.org",
                        200,
                        "text/plain",
                        "utf-8",
                        spool_file=body,
                    ),
                    ExtractInput(
                        "https://example.org", RenderMode.NEVER, ExtractResponseFormat.MAIN_MARKDOWN
                    ),
                    rendered=False,
                )
                assert result.extracted is not None and result.extracted.content == "collectible"
    finally:
        await worker.close()
        spool.close()
