"""Content-type routing and automatic browser fallback."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.extractors import (
    ExtractedContent,
    extract_html,
    normalize_text,
    should_render_html,
)


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
    body: bytes


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

        document = pymupdf.open(stream=data, filetype="pdf")
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


class ExtractEngine:
    def __init__(self, *, fetcher: Fetcher, browser: BrowserRenderer) -> None:
        self._fetcher = fetcher
        self._browser = browser

    async def extract(self, request: ExtractInput) -> ExtractDocument:
        rendered = request.render is RenderMode.ALWAYS
        fetched = (
            await self._browser.render(request) if rendered else await self._fetcher.fetch(request)
        )
        content_type = _mime(fetched.content_type)
        warnings: list[str] = []

        if content_type in {"text/html", "application/xhtml+xml"}:
            html = normalize_text(fetched.body, content_type, charset=fetched.charset)
            if request.render is RenderMode.AUTO:
                try:
                    quality_content = extract_html(
                        html,
                        source_url=fetched.final_url,
                        output=ExtractResponseFormat.MAIN_MARKDOWN,
                    ).content
                except ExtractError:
                    quality_content = ""
                if should_render_html(html, extracted_content=quality_content):
                    fetched = await self._browser.render(request)
                    content_type = _mime(fetched.content_type)
                    html = normalize_text(fetched.body, content_type, charset=fetched.charset)
                    rendered = True
            extracted = extract_html(
                html,
                source_url=fetched.final_url,
                output=request.output,
            )
        elif content_type == "application/pdf" or fetched.body.startswith(b"%PDF-"):
            extracted = _pdf(fetched.body)
        elif content_type.startswith("text/") or content_type in {
            "application/json",
            "application/xml",
            "application/xhtml+xml",
        }:
            content = normalize_text(fetched.body, content_type, charset=fetched.charset).strip()
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

        content_hash = hashlib.sha256(extracted.content.encode("utf-8")).hexdigest()
        return ExtractDocument(
            requested_url=fetched.requested_url,
            final_url=fetched.final_url,
            status_code=fetched.status_code,
            title=extracted.title,
            author=extracted.author,
            published_at=extracted.published_at,
            content_type=content_type,
            content=extracted.content,
            rendered=rendered,
            extractor=extracted.extractor,
            warnings=tuple(warnings),
            content_hash=content_hash,
            fetched_at=datetime.now(UTC),
        )


__all__ = ["ExtractDocument", "ExtractEngine", "ExtractInput", "FetchResult"]
