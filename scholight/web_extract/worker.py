"""Supervised parser/browser executable. Only small control frames use stdio."""

from __future__ import annotations

import asyncio
import gc
import json
import sys
import time
from contextlib import redirect_stdout
from dataclasses import asdict
from pathlib import Path
from typing import TextIO

from scholight.web_extract.engine import ExtractInput, FetchResult, parse_document
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.worker_contracts import WorkerFailure, WorkerJob


def _write_result(job: WorkerJob, value: dict[str, object] | bytes) -> int:
    written = 0
    chunks = (
        (value,)
        if isinstance(value, bytes)
        else (
            chunk.encode("utf-8")
            for chunk in json.JSONEncoder(ensure_ascii=False).iterencode(value)
        )
    )
    with Path(job.result_path).open("wb") as output:
        for chunk in chunks:
            written += len(chunk)
            if written > job.result_limit:
                raise ExtractError(
                    code="response_too_large",
                    message="Extracted result exceeds the output limit.",
                    status_code=413,
                    retryable=False,
                )
            output.write(chunk)
    return written


def _parse(job: WorkerJob, request: ExtractInput) -> dict[str, object]:
    import pymupdf
    from trafilatura.meta import reset_caches

    if job.fetched is None:
        raise ValueError("Missing fetched metadata")
    try:
        started_cpu = time.process_time()
        fetched = FetchResult(**job.fetched.model_dump(), body=Path(job.body_path).read_bytes())
        parsed = parse_document(
            fetched,
            request,
            rendered=job.rendered,
            reuse_quality=job.reuse_quality,
            fast_html=job.fast_html,
        )
        cpu_ms = (time.process_time() - started_cpu) * 1000
        size = _write_result(job, asdict(parsed))
        return {"size": size, "cpu_ms": cpu_ms}
    finally:
        reset_caches()
        pymupdf.TOOLS.store_shrink(100)  # type: ignore[no-untyped-call]


async def _main(kind: str, channel: TextIO) -> None:
    from scholight.web_extract.browser import PlaywrightBrowserRenderer

    browser = None
    try:
        if kind == "parser":
            # A native import failure is a startup/readiness failure, not a bad document.
            import pymupdf
            import pymupdf4llm

            # Referencing the native entry points verifies all optional imports loaded.
            _ = pymupdf.open, pymupdf4llm.to_markdown
            # Cache reset performs full GC after each parse. Import graphs are
            # process-lifetime state; scan them once before accepting any jobs.
            # New request objects remain collectible and are never frozen.
            gc.collect()
            gc.freeze()
        elif kind == "browser":
            from scholight.config import settings

            browser = PlaywrightBrowserRenderer(
                concurrency=1,
                timeout_seconds=settings.extract_render_timeout_seconds,
                max_content_bytes=settings.extract_max_download_bytes,
            )
            await browser.warmup()
        else:
            raise ValueError("Unknown worker kind")
        channel.write(json.dumps({"ready": True}) + "\n")
        channel.flush()
        while line := await asyncio.to_thread(sys.stdin.readline):
            try:
                job = WorkerJob.model_validate_json(line)
                request = ExtractInput(
                    url=str(job.request.url),
                    render=job.request.render,
                    output=job.request.output,
                    headers=job.request.headers,
                    cookies=job.request.cookies,
                )
                if browser is None:
                    result = _parse(job, request)
                else:
                    fetched = await browser.render(request)
                    size = _write_result(job, fetched.body)
                    result = {
                        "size": size,
                        "fetched": {
                            name: getattr(fetched, name)
                            for name in (
                                "requested_url",
                                "final_url",
                                "status_code",
                                "content_type",
                                "charset",
                            )
                        },
                    }
            except ExtractError as error:
                result = {
                    "error": WorkerFailure(
                        code=error.code,
                        message=error.message,
                        status_code=error.status_code,
                        retryable=error.retryable,
                    ).model_dump()
                }
            except Exception:
                result = {
                    "error": WorkerFailure(
                        code="extract_worker_failed",
                        message="Extraction worker failed.",
                        status_code=503,
                        retryable=True,
                    ).model_dump()
                }
            if browser is not None and browser.recycle_required:
                result["retire"] = True
            channel.write(json.dumps(result) + "\n")
            channel.flush()
    finally:
        if browser is not None:
            await browser.close()


if __name__ == "__main__":
    reply_channel = sys.stdout
    # Libraries may print diagnostics; never let them corrupt the control channel.
    with redirect_stdout(sys.stderr):
        asyncio.run(_main(sys.argv[1], reply_channel))
