"""Content-type routing and automatic browser fallback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from scholight.logging.emf import emit_emf
from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.extractors import (
    ExtractedContent,
    extract_html,
    normalize_text,
    should_render_html,
)
from scholight.web_extract.singleflight import Singleflight
from scholight.web_extract.telemetry import (
    ExtractTrace,
    current_trace,
    log_completion,
    mime_category,
    phase,
)

if TYPE_CHECKING:
    from scholight.web_extract.admission import Permit
    from scholight.web_extract.reservations import MemoryReservation
    from scholight.web_extract.spool import SpoolFile


@dataclass(frozen=True, slots=True)
class ExtractInput:
    url: str
    render: RenderMode
    output: ExtractResponseFormat
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    charset: str | None
    body: bytes = b""
    spool_file: SpoolFile | None = None
    permit: Permit | None = None
    reservation: MemoryReservation | None = None

    @property
    def source_bytes(self) -> int:
        return self.spool_file.size if self.spool_file is not None else len(self.body)

    def close(self) -> None:
        try:
            if self.spool_file is not None:
                self.spool_file.close()
        finally:
            try:
                if self.reservation is not None:
                    self.reservation.close()
            finally:
                if self.permit is not None:
                    self.permit.close()


@dataclass(frozen=True, slots=True)
class ExtractDocument:
    requested_url: str
    final_url: str
    status_code: int
    title: str | None
    author: str | None
    published_at: str | None
    content_type: str
    content: str
    rendered: bool
    extractor: str
    warnings: tuple[str, ...]
    content_hash: str
    fetched_at: datetime
    source_bytes: int


class Fetcher(Protocol):
    async def fetch(self, request: ExtractInput) -> FetchResult: ...


class BrowserRenderer(Protocol):
    async def render(self, request: ExtractInput) -> FetchResult: ...


def _mime(value: str) -> str:
    return value.partition(";")[0].strip().lower()


def _pdf(data: bytes) -> ExtractedContent:
    try:
        # Keep native PDF bindings out of the static HTML process until a PDF is requested.
        # This also preserves the package's existing lazy-import boundary.
        import pymupdf
        import pymupdf4llm

        with pymupdf.open(stream=data, filetype="pdf") as document:  # type: ignore[no-untyped-call]
            content = pymupdf4llm.to_markdown(document).strip()
            metadata = document.metadata or {}
    except Exception as exc:
        raise ExtractError(
            code="extraction_failed",
            message="PDF content could not be extracted.",
            status_code=422,
            retryable=False,
        ) from exc
    if not content:
        raise ExtractError(
            code="extraction_failed",
            message="PDF contains no readable text.",
            status_code=422,
            retryable=False,
        )
    return ExtractedContent(
        content=content,
        title=metadata.get("title") or None,
        author=metadata.get("author") or None,
        published_at=None,
        extractor="pymupdf4llm",
    )


@dataclass(frozen=True, slots=True)
class ParsedContent:
    extracted: ExtractedContent | None
    needs_render: bool = False


class Parser(Protocol):
    async def parse(
        self,
        fetched: FetchResult,
        request: ExtractInput,
        *,
        rendered: bool,
    ) -> ParsedContent: ...


@dataclass(frozen=True, slots=True)
class _StaticResult:
    document: ExtractDocument | None
    trace: ExtractTrace


def parse_document(
    fetched: FetchResult,
    request: ExtractInput,
    *,
    rendered: bool,
    reuse_quality: bool = True,
) -> ParsedContent:
    """Synchronous parsing entry point; production calls it only inside a worker."""
    content_type = _mime(fetched.content_type)
    data = fetched.spool_file.path.read_bytes() if fetched.spool_file is not None else fetched.body
    if content_type in {"text/html", "application/xhtml+xml"}:
        html = normalize_text(data, content_type, charset=fetched.charset)
        if request.render is RenderMode.AUTO and not rendered:
            quality_result = None
            quality_error = None
            try:
                quality_result = extract_html(
                    html,
                    source_url=fetched.final_url,
                    output=ExtractResponseFormat.MAIN_MARKDOWN,
                )
            except ExtractError as error:
                quality_error = error
            quality_content = quality_result.content if quality_result is not None else ""
            if should_render_html(html, extracted_content=quality_content):
                return ParsedContent(extracted=None, needs_render=True)
            if reuse_quality and request.output is ExtractResponseFormat.MAIN_MARKDOWN:
                if quality_error is not None:
                    raise quality_error
                return ParsedContent(extracted=quality_result)
        extracted = extract_html(html, source_url=fetched.final_url, output=request.output)
    elif content_type == "application/pdf" or data.startswith(b"%PDF-"):
        extracted = _pdf(data)
    elif content_type.startswith("text/") or content_type in {
        "application/json",
        "application/xml",
    }:
        content = normalize_text(data, content_type, charset=fetched.charset).strip()
        if not content:
            raise ExtractError(
                code="extraction_failed",
                message="Response contains no readable text.",
                status_code=422,
                retryable=False,
            )
        extracted = ExtractedContent(
            content=content,
            title=None,
            author=None,
            published_at=None,
            extractor="text",
        )
    else:
        raise ExtractError(
            code="unsupported_content_type",
            message=f"Content type {content_type or 'unknown'} is not supported.",
            status_code=415,
            retryable=False,
        )
    return ParsedContent(extracted=extracted)


class ExtractEngine:
    def __init__(
        self,
        *,
        fetcher: Fetcher,
        browser: BrowserRenderer,
        parser: Parser | None = None,
        admit: Callable[[], None] | None = None,
        singleflight: bool = False,
    ) -> None:
        self._fetcher = fetcher
        self._browser = browser
        self._parser = parser
        self._admit = admit or (lambda: None)
        self._flights = Singleflight[_StaticResult]() if singleflight else None

    async def _parse(
        self,
        fetched: FetchResult,
        request: ExtractInput,
        *,
        rendered: bool,
    ) -> ParsedContent:
        with phase("ParseLatency"):
            self._admit()
            if self._parser is not None:
                return await self._parser.parse(fetched, request, rendered=rendered)
            return parse_document(fetched, request, rendered=rendered)

    async def extract(self, request: ExtractInput) -> ExtractDocument:
        self._admit()
        if request.render is not RenderMode.ALWAYS:
            if self._flights is not None and not request.headers and not request.cookies:
                key = hashlib.sha256(
                    json.dumps((request.url, request.render.value, request.output.value)).encode()
                ).hexdigest()
                static = await self._flights.do(
                    key,
                    lambda work_id: self._shared_static(request, work_id),
                )
                if (trace := current_trace.get()) is not None:
                    trace.source_bytes = static.trace.source_bytes
                    trace.mime = static.trace.mime
                    trace.upstream_status = static.trace.upstream_status
                    if not trace.singleflight_joined:
                        trace.download_bytes += static.trace.download_bytes
                        trace.retry_count += static.trace.retry_count
                        trace.phases.update(static.trace.phases)
                document = static.document
            else:
                document = await self._perform(request, rendered=False)
            if document is not None:
                return document
        # Browser operations can include POST and always belong to this caller alone.
        self._admit()
        document = await self._perform(request, rendered=True)
        if document is None:
            raise RuntimeError("Parser returned no rendered document")
        return document

    async def _shared_static(self, request: ExtractInput, work_id: str) -> _StaticResult:
        # The work has its own bound; one short-lived waiter cannot expire others.
        trace = ExtractTrace(work_id, time.monotonic() + 52, static_work_id=work_id)
        context = current_trace.set(trace)
        outcome = "unexpected_error"
        try:
            async with asyncio.timeout(50):
                document = await self._perform(request, rendered=False)
            outcome = "needs_render" if document is None else "static_success"
            return _StaticResult(document, trace)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except TimeoutError as error:
            outcome = "error_extract_timeout"
            raise ExtractError(
                code="extract_timeout",
                message="Static extraction exceeded its deadline.",
                status_code=504,
                retryable=True,
            ) from error
        except ExtractError as error:
            outcome = f"error_{error.code}"
            raise
        finally:
            emit_emf(
                service="extract",
                metrics={
                    "StaticWorkCount": (1, "Count"),
                    "StaticWorkDownloadBytes": (trace.download_bytes, "Bytes"),
                },
            )
            log_completion(
                trace=trace,
                scope="static_work",
                outcome=outcome,
                render=request.render.value,
                cache_eligible=True,
                cache_hit=False,
            )
            current_trace.reset(context)

    async def close(self) -> None:
        if self._flights is not None:
            await self._flights.close()

    async def _perform(self, request: ExtractInput, *, rendered: bool) -> ExtractDocument | None:
        with phase("RenderLatency" if rendered else "DownloadLatency"):
            fetched = (
                await self._browser.render(request)
                if rendered
                else await self._fetcher.fetch(request)
            )
        trace = current_trace.get()
        if trace is not None:
            trace.upstream_status = fetched.status_code
            trace.mime = mime_category(fetched.content_type)
            if rendered:
                trace.dom_bytes = fetched.source_bytes
            else:
                trace.source_bytes = fetched.source_bytes
        try:
            parsed = await self._parse(fetched, request, rendered=rendered)
            if parsed.needs_render:
                return None
            if parsed.extracted is None:
                raise RuntimeError("Parser returned no document")
            return self._document(fetched, parsed.extracted, rendered=rendered)
        finally:
            fetched.close()

    @staticmethod
    def _document(
        fetched: FetchResult,
        extracted: ExtractedContent,
        *,
        rendered: bool,
    ) -> ExtractDocument:
        content_hash = hashlib.sha256(extracted.content.encode("utf-8")).hexdigest()
        return ExtractDocument(
            requested_url=fetched.requested_url,
            final_url=fetched.final_url,
            status_code=fetched.status_code,
            title=extracted.title,
            author=extracted.author,
            published_at=extracted.published_at,
            content_type=_mime(fetched.content_type),
            content=extracted.content,
            rendered=rendered,
            extractor=extracted.extractor,
            warnings=(),
            content_hash=content_hash,
            fetched_at=datetime.now(UTC),
            source_bytes=fetched.source_bytes,
        )


__all__ = ["ExtractDocument", "ExtractEngine", "ExtractInput", "FetchResult"]
