"""Deterministic server-side Survey report PDF rendering.

The HTML/CSS assembly is pure Python and testable everywhere; the WeasyPrint
native backend (Pango) is loaded lazily so hosts without the system libraries
can still import the API package and run the non-rendering test suite.
"""

from __future__ import annotations

import base64
import re
from datetime import date
from html import escape, unescape
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import markdown as markdown_lib

_FONT_FILES = (
    ("Literata", 400, "normal", "Literata-400.ttf"),
    ("Literata", 400, "italic", "Literata-Italic-400.ttf"),
    ("Literata", 600, "normal", "Literata-600.ttf"),
    ("Manrope", 400, "normal", "Manrope-400.ttf"),
    ("Manrope", 600, "normal", "Manrope-600.ttf"),
)

_FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

_IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

_HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.DOTALL)
_IMG_TAG_PATTERN = re.compile(r"<img\b[^>]*>")
_SRC_ATTRIBUTE_PATTERN = re.compile(r"""\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_ALT_ATTRIBUTE_PATTERN = re.compile(r"""\balt\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
_LEADING_H1_PATTERN = re.compile(r"^\s*<h1[^>]*>(.*?)</h1>", re.DOTALL)
_INNER_TAG_PATTERN = re.compile(r"<[^>]+>")
_UNSAFE_BODY_TAGS = ("script", "style", "iframe", "object", "embed", "link", "base", "form")
_STYLE_ATTRIBUTE_PATTERN = re.compile(
    r"\s(?:style|on[a-z]+)\s*=\s*(?:\"[^\"]*\"|'[^']*')",
    re.IGNORECASE,
)
_MATH_DISPLAY_PATTERN = re.compile(r"(?<!\\)\$\$(?P<formula>.+?)(?<!\\)\$\$", re.DOTALL)
_MATH_INLINE_PATTERN = re.compile(r"(?<!\\)\$(?!\$)(?P<formula>[^$\n]+?)(?<!\\)\$")
_MATH_MAX_CHARS = 2_000
_FIGURE_CAPTION_PATTERN = re.compile(
    r"(<p><img(?![^>]*class=\"math-)[^>]*>)</p>\s*<p><em>([^<]+)</em></p>",
)

# Print typography mirrors the web report (DESIGN.md): Literata carries the
# wordmark and headings, Manrope carries body copy, and the single accent is
# the Scholight brand blue.
_PRINT_CSS = """
@page {
  size: A4;
  margin: 22mm 17mm 20mm 17mm;
  @top-left {
    content: "scholight";
    font-family: 'Literata';
    font-weight: 600;
    font-size: 10.5pt;
    letter-spacing: 0.01em;
    color: #0E0F14;
    vertical-align: bottom;
    width: 100%;
    padding-bottom: 2.5mm;
    border-bottom: 0.6pt solid #1F45B8;
  }
  @bottom-right {
    content: counter(page) " / " counter(pages);
    font-family: 'Manrope';
    font-size: 8.5pt;
    color: #61636E;
  }
}
@page cover {
  margin: 24mm 20mm;
  @top-left { content: none; border: none; }
  @bottom-right { content: none; }
}
html { font-size: 10pt; }
body {
  font-family: 'Manrope';
  font-size: 10pt;
  line-height: 1.7;
  color: #2E2F36;
  margin: 0;
}
.cover {
  page: cover;
  page-break-after: always;
}
.cover-wordmark {
  font-family: 'Literata';
  font-weight: 600;
  font-size: 13pt;
  color: #0E0F14;
  display: block;
  width: 100%;
  padding-bottom: 2.5mm;
  border-bottom: 0.6pt solid #1F45B8;
}
.cover-kicker {
  font-family: 'Manrope';
  font-weight: 600;
  font-size: 9pt;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: #1F45B8;
  margin: 22mm 0 0;
}
.cover-title {
  font-family: 'Literata';
  font-weight: 600;
  font-size: 26pt;
  line-height: 1.25;
  letter-spacing: -0.01em;
  color: #0E0F14;
  margin: 4mm 0 0;
}
.cover-meta {
  font-family: 'Manrope';
  font-size: 10.5pt;
  color: #61636E;
  margin-top: 6mm;
}
.report h1, .report h2, .report h3, .report h4 {
  font-family: 'Literata';
  font-weight: 600;
  color: #0E0F14;
  line-height: 1.3;
  page-break-after: avoid;
}
.report h1 { font-size: 17pt; margin: 1.6em 0 0.6em; }
.report h2 {
  font-size: 13.5pt;
  margin: 1.8em 0 0.55em;
  padding-bottom: 1.5pt;
  border-bottom: 0.5pt solid #DBD9CC;
}
.report h3 { font-size: 11.5pt; margin: 1.5em 0 0.5em; }
.report h4 { font-size: 10.5pt; margin: 1.3em 0 0.45em; }
.report p { margin: 0 0 0.85em; orphans: 3; widows: 3; }
.report a { color: #1F45B8; text-decoration: none; }
.report blockquote {
  margin: 1.1em 0;
  padding: 1mm 0 1mm 5mm;
  border-left: 1.5pt solid #CCC9BD;
  color: #61636E;
}
.report code {
  font-family: 'Menlo', 'DejaVu Sans Mono', monospace;
  font-size: 8.8pt;
  background: #F4F2EC;
  padding: 0.5pt 2pt;
  border-radius: 2pt;
}
.report pre {
  background: #F4F2EC;
  border: 0.5pt solid #DBD9CC;
  border-radius: 3pt;
  padding: 3mm;
  font-size: 8.5pt;
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: break-word;
  page-break-inside: avoid;
}
.report pre code { background: none; padding: 0; }
.report table {
  width: 100%;
  border-collapse: collapse;
  margin: 1.2em 0;
  font-size: 9pt;
}
.report th {
  font-family: 'Manrope';
  font-weight: 600;
  color: #0E0F14;
  text-align: left;
  border-bottom: 1pt solid #0E0F14;
  padding: 2mm 2.5mm 1.5mm;
}
.report td {
  border-bottom: 0.5pt solid #DBD9CC;
  padding: 1.8mm 2.5mm;
  vertical-align: top;
}
.report tr { page-break-inside: avoid; }
.report img {
  max-width: 100%;
  display: block;
  margin: 4mm auto 2mm;
}
.report img.math-inline {
  display: inline;
  width: auto;
  max-height: 1.6em;
  vertical-align: -0.3em;
  margin: 0 0.12em;
}
.report img.math-display {
  width: auto;
  max-width: 100%;
  margin: 4mm auto;
}
.report .math-fallback {
  font-family: 'DejaVu Sans Mono', monospace;
  white-space: pre-wrap;
}
.report .chart-caption {
  color: #61636E;
  font-size: 9pt;
  text-align: center;
  margin: 0 0 4mm;
}
.report ul, .report ol { margin: 0 0 0.9em; padding-left: 6mm; }
.report li { margin-bottom: 0.3em; }
.report hr { border: none; border-top: 0.5pt solid #DBD9CC; margin: 2em 0; }
"""

_WORDMARK = "scholight"


class ReportPdfError(Exception):
    """A Survey report PDF could not be produced."""


def _font_faces() -> str:
    faces = [
        f"@font-face {{ font-family: '{family}'; font-weight: {weight}; "
        f"font-style: {style}; src: url('{(_FONTS_DIR / filename).as_uri()}'); }}"
        for family, weight, style, filename in _FONT_FILES
    ]
    return "\n".join(faces)


def _first_attribute(pattern: re.Pattern[str], tag: str) -> str | None:
    match = pattern.search(tag)
    if match is None:
        return None
    return next((value for value in match.groups() if value is not None), None)


def _resolve_image(src: str | None, images: dict[str, bytes]) -> tuple[bytes, str] | None:
    """Map a Markdown image reference onto a manifest-authorized asset."""
    if not src or re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", src) or src.startswith(("/", "//")):
        return None
    clean = src.split("#", 1)[0].split("?", 1)[0]
    parts: list[str] = []
    for part in clean.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return None
        parts.append(part)
    path = "/".join(parts)
    data = images.get(path)
    if data is None:
        return None
    mime = _IMAGE_MIME_BY_SUFFIX.get(PurePosixPath(path).suffix.lower())
    if mime is None:
        return None
    return data, mime


def _embed_images(body_html: str, images: dict[str, bytes]) -> str:
    def replace(match: re.Match[str]) -> str:
        tag = match.group(0)
        src = _first_attribute(_SRC_ATTRIBUTE_PATTERN, tag)
        if "math-inline" in tag or "math-display" in tag:
            return tag
        resolved = _resolve_image(src, images)
        if resolved is None:
            return ""
        data, mime = resolved
        encoded = base64.b64encode(data).decode("ascii")
        alt = _first_attribute(_ALT_ATTRIBUTE_PATTERN, tag)
        alt_attribute = f' alt="{escape(alt)}"' if alt is not None else ""
        return f'<img src="data:{mime};base64,{encoded}"{alt_attribute} />'

    return _IMG_TAG_PATTERN.sub(replace, body_html)


def _sanitize_body_html(body_html: str) -> str:
    """Remove active/raw HTML that could trigger network or script side effects."""
    sanitized = body_html
    for tag in _UNSAFE_BODY_TAGS:
        sanitized = re.sub(
            rf"<{tag}\b[^>]*>.*?(?:</{tag}\s*>|$)",
            "",
            sanitized,
            flags=re.IGNORECASE | re.DOTALL,
        )
        sanitized = re.sub(rf"<{tag}\b[^>]*/?>", "", sanitized, flags=re.IGNORECASE)
    return _STYLE_ATTRIBUTE_PATTERN.sub("", sanitized)


def _render_math_formula(formula: str, *, display: bool) -> str:
    """Render a bounded LaTeX formula to a self-contained PNG or safe text."""
    normalized = formula.strip()
    delimiter = "$$" if display else "$"
    fallback = escape(f"{delimiter}{normalized}{delimiter}")
    if not normalized or len(normalized) > _MATH_MAX_CHARS:
        return f'<span class="math-fallback">{fallback}</span>'
    try:
        from matplotlib.mathtext import math_to_image

        output = BytesIO()
        math_to_image(f"${normalized}$", output, format="png", dpi=180)
    except Exception:
        return f'<span class="math-fallback">{fallback}</span>'
    encoded = base64.b64encode(output.getvalue()).decode("ascii")
    class_name = "math-display" if display else "math-inline"
    return (
        f'<img class="{class_name}" src="data:image/png;base64,{encoded}" '
        f'alt="{escape(normalized)}" />'
    )


def _render_math_markdown(markdown_text: str) -> str:
    """Replace Markdown dollar-delimited formulas before Markdown conversion."""
    rendered: dict[tuple[str, bool], str] = {}

    def replace(match: re.Match[str], *, display: bool) -> str:
        formula = match.group("formula")
        key = (formula, display)
        if key not in rendered:
            rendered[key] = _render_math_formula(formula, display=display)
        return rendered[key]

    markdown_text = _MATH_DISPLAY_PATTERN.sub(
        lambda match: replace(match, display=True), markdown_text
    )
    return _MATH_INLINE_PATTERN.sub(lambda match: replace(match, display=False), markdown_text)


def _normalized_heading_text(heading_html: str) -> str:
    return " ".join(unescape(_INNER_TAG_PATTERN.sub("", heading_html)).split())


def _drop_leading_duplicate_title(body_html: str, title: str) -> str:
    leading = _LEADING_H1_PATTERN.match(body_html)
    if leading is None:
        return body_html
    if _normalized_heading_text(leading.group(1)) != " ".join(title.split()):
        return body_html
    return body_html[leading.end() :].lstrip()


def _format_cover_date(generated_on: date) -> str:
    return f"{generated_on:%B} {generated_on.day}, {generated_on.year}"


def build_report_html(
    *,
    title: str,
    markdown_text: str,
    images: dict[str, bytes],
    generated_on: date,
) -> str:
    """Assemble the self-contained print HTML document for one Survey report."""
    from scholight.survey.math_format import normalize_report_math

    cleaned_markdown = normalize_report_math(_HTML_COMMENT_PATTERN.sub("", markdown_text))
    body_html = markdown_lib.markdown(
        _render_math_markdown(cleaned_markdown),
        extensions=["tables", "fenced_code"],
        output_format="html5",
    )
    body_html = _FIGURE_CAPTION_PATTERN.sub(
        lambda match: f'{match.group(1)}<p class="chart-caption">{match.group(2)}</p>',
        body_html,
    )
    body_html = _embed_images(body_html, images)
    body_html = _sanitize_body_html(body_html)
    body_html = _drop_leading_duplicate_title(body_html, title)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8" />\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>\n{_font_faces()}\n{_PRINT_CSS}\n</style>\n"
        "</head>\n"
        "<body>\n"
        '<section class="cover">\n'
        f'<span class="cover-wordmark">{_WORDMARK}</span>\n'
        '<p class="cover-kicker">Survey Report</p>\n'
        f'<h1 class="cover-title">{escape(title)}</h1>\n'
        f'<p class="cover-meta">Generated by Scholight · {_format_cover_date(generated_on)}</p>\n'
        "</section>\n"
        f'<article class="report">\n{body_html}\n</article>\n'
        "</body>\n"
        "</html>\n"
    )


def _load_weasyprint() -> Any:
    import weasyprint

    return weasyprint


def render_report_pdf(
    *,
    title: str,
    markdown_text: str,
    images: dict[str, bytes],
    generated_on: date,
) -> bytes:
    """Render the final Survey report as a branded PDF document."""
    html = build_report_html(
        title=title,
        markdown_text=markdown_text,
        images=images,
        generated_on=generated_on,
    )
    try:
        weasyprint = _load_weasyprint()
    except (ImportError, OSError) as exc:
        raise ReportPdfError("PDF backend is unavailable on this host") from exc

    default_url_fetcher = weasyprint.default_url_fetcher

    def safe_url_fetcher(url: str, *args: Any, **kwargs: Any) -> Any:
        parsed = urlsplit(url)
        if parsed.scheme == "data":
            return default_url_fetcher(url, *args, **kwargs)
        if parsed.scheme == "file":
            font_path = Path(unquote(parsed.path)).resolve()
            try:
                font_path.relative_to(_FONTS_DIR.resolve())
            except ValueError as exc:
                raise ValueError("PDF resources must be bundled assets") from exc
            return default_url_fetcher(url, *args, **kwargs)
        raise ValueError("PDF resources must be bundled assets")

    try:
        return bytes(weasyprint.HTML(string=html, url_fetcher=safe_url_fetcher).write_pdf())
    except OSError as exc:
        raise ReportPdfError("PDF backend is unavailable on this host") from exc
    except ReportPdfError:
        raise
    except Exception as exc:
        raise ReportPdfError("Survey report PDF could not be rendered") from exc


__all__ = [
    "ReportPdfError",
    "build_report_html",
    "render_report_pdf",
]
