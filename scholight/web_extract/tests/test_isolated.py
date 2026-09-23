from __future__ import annotations

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
