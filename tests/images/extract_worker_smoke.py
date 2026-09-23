"""Real Linux process groups and file-based PDF parsing in the final image."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pymupdf

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.engine import ExtractInput, FetchResult
from scholight.web_extract.isolated import IsolatedParser
from scholight.web_extract.memory import read_cgroup
from scholight.web_extract.process_family import enable_subreaping, process_groups
from scholight.web_extract.spool import Spool
from scholight.web_extract.worker_supervisor import WorkerSupervisor


def _remaining(groups: set[int]) -> list[int]:
    remaining = []
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            group = int(path.read_text().rsplit(")", 1)[1].split()[2])
            if group in groups:
                remaining.append(int(path.parent.name))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return remaining


async def main() -> None:
    enable_subreaping()
    spool = Spool(Path("/data/worker-smoke"))
    spool.start()
    parser = WorkerSupervisor("parser")
    browser = WorkerSupervisor("browser")
    try:
        await parser.warmup()
        await browser.warmup()
        assert browser.pid is not None
        groups = process_groups(browser.pid)
        assert len(groups) >= 2, "Chromium must launch a real detached process group"
        assert read_cgroup().working_set < 640 * 1024 * 1024
        with pymupdf.open() as document:
            document.new_page().insert_text((72, 72), "Scholight isolated PDF worker works.")
            data = document.tobytes()
        adapter = IsolatedParser(parser, spool, max_output_bytes=1024 * 1024)
        with spool.allocate(len(data)) as body:
            body.write(data)
            result = await adapter.parse(
                FetchResult(
                    requested_url="https://example.com/fixture.pdf",
                    final_url="https://example.com/fixture.pdf",
                    status_code=200,
                    content_type="application/pdf",
                    charset=None,
                    spool_file=body,
                ),
                ExtractInput(
                    url="https://example.com/fixture.pdf",
                    render=RenderMode.NEVER,
                    output=ExtractResponseFormat.MAIN_MARKDOWN,
                ),
                rendered=False,
            )
            assert result.extracted is not None
            assert "Scholight isolated PDF worker works." in result.extracted.content
        assert spool.reserved_bytes == 0
        await browser.close()
        await asyncio.sleep(0.05)
        assert not _remaining(groups), "Worker shutdown left Chromium descendants or zombies"
    finally:
        await asyncio.gather(parser.close(), browser.close())
        spool.close()


if __name__ == "__main__":
    asyncio.run(main())
