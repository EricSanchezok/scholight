"""Async adapters to the supervised serial parser and independent browser."""

from __future__ import annotations

import json
from contextlib import ExitStack

from scholight.web_extract.contracts import InternalExtractRequest
from scholight.web_extract.engine import ExtractInput, FetchResult, ParsedContent
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.extractors import ExtractedContent
from scholight.web_extract.reservations import MemoryBudget
from scholight.web_extract.spool import Spool, SpoolFile
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
        body_path = str(fetched.spool_file.path)
        result: SpoolFile | None = None
        is_pdf = False
        with ExitStack() as scratch:

            def prepare() -> dict[str, object]:
                nonlocal result, is_pdf
                with open(body_path, "rb") as source:
                    is_pdf = source.read(5) == b"%PDF-"
                is_pdf = (
                    is_pdf or fetched.content_type.partition(";")[0].lower() == "application/pdf"
                )
                if fetched.reservation is not None:
                    fetched.reservation.transfer(
                        "parse",
                        size=fetched.source_bytes,
                        mime="application/pdf" if is_pdf else fetched.content_type,
                        warm=is_pdf and self._worker.phase_warm("pdf"),
                    )
                result = scratch.enter_context(self._spool.allocate(self._max_output_bytes))
                return WorkerJob(
                    request=_request(request),
                    body_path=body_path,
                    fetched=FetchMetadata.model_validate(
                        {name: getattr(fetched, name) for name in FetchMetadata.model_fields}
                    ),
                    rendered=rendered,
                    reuse_quality=self._reuse_quality,
                    result_path=str(result.path),
                    result_limit=result.limit,
                ).model_dump(mode="json")

            reply = await self._worker.call(prepare)
            _raise_failure(reply)
            if is_pdf:
                self._worker.mark_phase_warm("pdf")
            cpu_ms = reply.get("cpu_ms")
            if (trace := current_trace.get()) is not None and isinstance(cpu_ms, (int, float)):
                trace.phases["ParseCPU"] = trace.phases.get("ParseCPU", 0) + cpu_ms
            if result is None:
                raise RuntimeError("Parser did not allocate its result")
            data = json.loads(result.path.read_bytes())
            return ParsedContent(
                extracted=ExtractedContent(**data["extracted"]) if data["extracted"] else None,
                needs_render=bool(data["needs_render"]),
            )


class IsolatedBrowser:
    def __init__(
        self,
        worker: WorkerSupervisor,
        spool: Spool,
        *,
        max_content_bytes: int,
        memory: MemoryBudget | None = None,
    ) -> None:
        self._worker = worker
        self._spool = spool
        self._max_content_bytes = max_content_bytes
        self._memory = memory

    async def render(self, request: ExtractInput) -> FetchResult:
        body: SpoolFile | None = None
        reservation = self._memory.lease() if self._memory is not None else None
        try:

            def prepare() -> dict[str, object]:
                nonlocal body
                if reservation is not None:
                    reservation.transfer("browser", warm=self._worker.phase_warm("browser"))
                body = self._spool.allocate(self._max_content_bytes)
                return WorkerJob(
                    request=_request(request),
                    result_path=str(body.path),
                    result_limit=body.limit,
                ).model_dump(mode="json")

            reply = await self._worker.call(prepare)
            _raise_failure(reply)
            self._worker.mark_phase_warm("browser")
            if body is None:
                raise RuntimeError("Browser did not allocate its result")
            body.size = body.path.stat().st_size
            if body.size > body.limit:
                raise RuntimeError("Browser exceeded its scratch allowance")
            body.seal()
            if reservation is not None:
                # Browser context has closed; only its output file remains while queued.
                reservation.transfer("download", size=body.size)
            metadata = FetchMetadata.model_validate(reply["fetched"])
            return FetchResult(**metadata.model_dump(), spool_file=body, reservation=reservation)
        except BaseException:
            if body is not None:
                body.close()
            if reservation is not None:
                reservation.close()
            raise
