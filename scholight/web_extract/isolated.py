"""Async adapters to the supervised serial parser and independent browser."""

from __future__ import annotations

import json

from scholight.web_extract.contracts import InternalExtractRequest
from scholight.web_extract.engine import ExtractInput, FetchResult, ParsedContent
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.extractors import ExtractedContent
from scholight.web_extract.spool import Spool
from scholight.web_extract.telemetry import current_trace
from scholight.web_extract.worker_contracts import FetchMetadata, WorkerFailure, WorkerJob
from scholight.web_extract.worker_supervisor import WorkerSupervisor


def _request(request: ExtractInput) -> InternalExtractRequest:
    return InternalExtractRequest.model_validate(
        {
            "url": request.url,
            "render": request.render,
            "output": request.output,
            "headers": request.headers,
            "cookies": request.cookies,
        }
    )


def _raise_failure(reply: dict[str, object]) -> None:
    if "error" in reply:
        error = WorkerFailure.model_validate(reply["error"])
        raise ExtractError(**error.model_dump())


class IsolatedParser:
    def __init__(
        self,
        worker: WorkerSupervisor,
        spool: Spool,
        *,
        max_output_bytes: int,
        reuse_quality: bool = True,
    ) -> None:
        self._worker = worker
        self._spool = spool
        self._max_output_bytes = max_output_bytes
        self._reuse_quality = reuse_quality

    async def parse(
        self,
        fetched: FetchResult,
        request: ExtractInput,
        *,
        rendered: bool,
    ) -> ParsedContent:
        if fetched.spool_file is None:
            raise RuntimeError("Isolated parsing requires a spooled document")
        with self._spool.allocate(self._max_output_bytes) as result:
            job = WorkerJob(
                request=_request(request),
                body_path=str(fetched.spool_file.path),
                fetched=FetchMetadata.model_validate(
                    {name: getattr(fetched, name) for name in FetchMetadata.model_fields}
                ),
                rendered=rendered,
                reuse_quality=self._reuse_quality,
                result_path=str(result.path),
                result_limit=result.limit,
            )
            reply = await self._worker.call(job.model_dump(mode="json"))
            _raise_failure(reply)
            cpu_ms = reply.get("cpu_ms")
            if (trace := current_trace.get()) is not None and isinstance(cpu_ms, (int, float)):
                trace.phases["ParseCPU"] = trace.phases.get("ParseCPU", 0) + cpu_ms
            data = json.loads(result.path.read_bytes())
            return ParsedContent(
                extracted=ExtractedContent(**data["extracted"]) if data["extracted"] else None,
                needs_render=bool(data["needs_render"]),
            )


class IsolatedBrowser:
    def __init__(self, worker: WorkerSupervisor, spool: Spool, *, max_content_bytes: int) -> None:
        self._worker = worker
        self._spool = spool
        self._max_content_bytes = max_content_bytes

    async def render(self, request: ExtractInput) -> FetchResult:
        body = self._spool.allocate(self._max_content_bytes)
        try:
            job = WorkerJob(
                request=_request(request),
                result_path=str(body.path),
                result_limit=body.limit,
            )
            reply = await self._worker.call(job.model_dump(mode="json"))
            _raise_failure(reply)
            body.size = body.path.stat().st_size
            if body.size > body.limit:
                raise RuntimeError("Browser exceeded its scratch allowance")
            metadata = FetchMetadata.model_validate(reply["fetched"])
            return FetchResult(**metadata.model_dump(), spool_file=body)
        except BaseException:
            body.close()
            raise
