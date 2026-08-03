"""Deterministic HTML, text, and metadata extraction."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser

import markdownify
import trafilatura

from scholight.models.web_extract import ExtractResponseFormat
from scholight.web_extract.errors import ExtractError

_SPA_MARKERS = re.compile(
    r'id=["\'](?:root|app|__next)["\']|__NEXT_DATA__|ng-version|data-reactroot',
    re.IGNORECASE,
)
_SCRIPT = re.compile(r"<script\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExtractedContent:
    content: str
    title: str | None
    author: str | None
    published_at: str | None
    extractor: str


class _MetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.author: str | None = None
        self.published_at: str | None = None
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self._in_title = True
            return
        if tag.lower() != "meta":
            return
        values = {name.lower(): value for name, value in attrs if value is not None}
        key = (values.get("name") or values.get("property") or "").lower()
        content = values.get("content")
        if content is None:
            return
        if key in {"author", "article:author"} and self.author is None:
            self.author = content.strip() or None
        if key in {"article:published_time", "date", "datepublished"} and self.published_at is None:
            self.published_at = content.strip() or None

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
            title = "".join(self._title_parts).strip()
            self.title = title or None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)


def _metadata(html: str) -> _MetadataParser:
    parser = _MetadataParser()
    parser.feed(html)
    return parser


def extract_html(
    html: str,
    *,
    source_url: str,
    output: ExtractResponseFormat,
) -> ExtractedContent:
    metadata = _metadata(html)
    if output is ExtractResponseFormat.RAW_HTML:
        content = html
        extractor = "raw_html"
    elif output is ExtractResponseFormat.FULL_MARKDOWN:
        content = markdownify.markdownify(
            html,
            heading_style=markdownify.ATX,
            strip=["script", "style", "noscript"],
        ).strip()
        extractor = "markdownify"
    else:
        output_format = "txt" if output is ExtractResponseFormat.TEXT else "markdown"
        content = (
            trafilatura.extract(
                html,
                url=source_url,
                output_format=output_format,
                include_comments=False,
                include_images=True,
                include_links=True,
                include_tables=True,
                favor_recall=True,
            )
            or ""
        ).strip()
        extractor = "trafilatura"
    if not content:
        raise ExtractError(
            code="extraction_failed",
            message="No readable content could be extracted from the page.",
            status_code=422,
            retryable=False,
        )
    return ExtractedContent(
        content=content,
        title=metadata.title,
        author=metadata.author,
        published_at=metadata.published_at,
        extractor=extractor,
    )


def should_render_html(html: str, *, extracted_content: str) -> bool:
    meaningful_length = len(re.sub(r"\s+", " ", extracted_content).strip())
    if meaningful_length >= 300:
        return False
    lowered = html.lower()
    return bool(
        _SPA_MARKERS.search(html)
        or _SCRIPT.search(html)
        or "enable javascript" in lowered
        or "javascript is required" in lowered
    )


def normalize_text(data: bytes, content_type: str, *, charset: str | None) -> str:
    encoding = charset or "utf-8"
    try:
        text = data.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        text = data.decode("utf-8", errors="replace")
    if content_type in {"application/json", "text/json"}:
        try:
            return json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            return text
    return text


__all__ = ["ExtractedContent", "extract_html", "normalize_text", "should_render_html"]
