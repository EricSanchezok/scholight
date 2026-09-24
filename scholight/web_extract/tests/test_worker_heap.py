from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.engine import ExtractInput, FetchResult
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.isolated import IsolatedParser
from scholight.web_extract.spool import Spool
from scholight.web_extract.worker_supervisor import WorkerSupervisor


@pytest.mark.asyncio
async def test_heap_release_runs_after_success_and_error_frames_are_released(
    tmp_path: Path,
) -> None:
    # The child owns GC startup state. Its maintenance boundary must see no live
    # parse-local document, including an exception traceback retaining that frame.
    program = """
import asyncio, sys, weakref
from contextlib import redirect_stdout
from scholight.web_extract import worker
from scholight.web_extract.errors import ExtractError
class Payload: pass
previous = None
trimmed = 0
parsed = 0
def parse(job, request):
    global previous, parsed
    assert trimmed == parsed, 'Previous request did not release its heap'
    parsed += 1
    payload = Payload()
    previous = weakref.ref(payload)
    if request.url.endswith('/bad'):
        raise ExtractError(code='extraction_failed', message='Invalid document',
                           status_code=422, retryable=False)
    worker._write_result(job, {'extracted': {
        'content': 'released', 'title': None, 'author': None,
        'published_at': None, 'extractor': 'text'}, 'needs_render': False})
    return {}
def release():
    global trimmed
    assert previous() is None, 'Parse frame is still holding a document'
    trimmed += 1
worker._parse = parse
worker.release_unused_heap = release
channel = sys.stdout
with redirect_stdout(sys.stderr):
    asyncio.run(worker._main('parser', channel))
"""
    worker = WorkerSupervisor("parser", command=(sys.executable, "-u", "-c", program))
    spool = Spool(tmp_path)
    spool.start()
    parser = IsolatedParser(worker, spool, max_output_bytes=10_000)
    try:
        with spool.allocate(100) as body:
            body.write(b"Heap ownership probe")
            for path in ("first", "bad", "last"):
                url = "https://example.org/" + path
                fetched = FetchResult(url, url, 200, "text/plain", "utf-8", spool_file=body)
                request = ExtractInput(url, RenderMode.NEVER, ExtractResponseFormat.MAIN_MARKDOWN)
                if path == "bad":
                    with pytest.raises(ExtractError, match="Invalid document"):
                        await parser.parse(fetched, request, rendered=False)
                else:
                    result = await parser.parse(fetched, request, rendered=False)
                    assert result.extracted is not None and result.extracted.content == "released"
    finally:
        await worker.close()
        spool.close()
