"""Content-type routing and automatic browser fallback."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.extractors import (
    ExtractedContent,
    extract_html,
    normalize_text,
    should_render_html,
)
from scholight.web_extract.telemetry import current_trace, mime_category, phase

if TYPE_CHECKING:
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

    @property
    def source_bytes(self) -> int:
        return self.spool_file.size if self.spool_file is not None else len(self.body)

    def close(self) -> None:
        if self.spool_file is not None:
            self.spool_file.close()


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


def parse_document(
    fetched: FetchResult,
    request: ExtractInput,
    *,
    rendered: bool,
) -> ParsedContent:
    """Synchronous parsing entry point; production calls it only inside a worker."""
    content_type = _mime(fetched.content_type)
    data = fetched.spool_file.path.read_bytes() if fetched.spool_file is not None else fetched.body
    if content_type in {"text/html", "application/xhtml+xml"}:
        html = normalize_text(data, content_type, charset=fetched.charset)
        if request.render is RenderMode.AUTO and not rendered:
            try:
                quality_content = extract_html(
                    html,
                    source_url=fetched.final_url,
                    output=ExtractResponseFormat.MAIN_MARKDOWN,
                ).content
            except ExtractError:
                quality_content = ""
            if should_render_html(html, extracted_content=quality_content):
                return ParsedContent(extracted=None, needs_render=True)
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
    ) -> None:
        self._fetcher = fetcher
        self._browser = browser
        self._parser = parser
        self._admit = admit or (lambda: None)

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
        rendered = request.render is RenderMode.ALWAYS
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
                fetched.close()
                self._admit()
                with phase("RenderLatency"):
                    fetched = await self._browser.render(request)
                if trace is not None:
                    trace.upstream_status = fetched.status_code
                    trace.dom_bytes = fetched.source_bytes
                    trace.mime = mime_category(fetched.content_type)
                rendered = True
                parsed = await self._parse(fetched, request, rendered=True)
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
