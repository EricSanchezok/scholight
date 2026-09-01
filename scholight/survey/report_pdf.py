"""Deterministic server-side Survey report PDF rendering.

The HTML/CSS assembly is pure Python and testable everywhere; the WeasyPrint
native backend (Pango) is loaded lazily so hosts without the system libraries
can still import the API package and run the non-rendering test suite.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from datetime import date
from html import escape, unescape
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import uuid4

import markdown as markdown_lib

_FONT_FILES = (
    ("Literata", 400, "normal", "Literata-400.ttf"),
    ("Literata", 400, "italic", "Literata-Italic-400.ttf"),
    ("Literata", 600, "normal", "Literata-600.ttf"),
    ("Manrope", 400, "normal", "Manrope-400.ttf"),
    ("Manrope", 600, "normal", "Manrope-600.ttf"),
)

_FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
_KATEX_DIR = Path(__file__).resolve().parent / "assets" / "katex"

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
_MATH_TOKEN_PREFIX = "MATHTOKEN_"
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
  font-family: 'Manrope', 'Noto Sans CJK SC', sans-serif;
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
  font-family: 'Literata', 'Noto Serif CJK SC', serif;
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
  font-family: 'Literata', 'Noto Serif CJK SC', serif;
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
.katex {
  /* KaTeX inline formulas sit slightly high against Manrope body text in
     WeasyPrint; nudge the math axis down to match the text baseline. */
  vertical-align: -0.15em;
}
.katex-display .katex {
  /* Display math is a block element; the vertical-align nudge must not
     apply inside centered display blocks. */
  vertical-align: baseline;
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


def _katex_css() -> str:
    """Return the vendored KaTeX layout stylesheet for the print document."""
    return (_KATEX_DIR / "katex.css").read_text(encoding="utf-8")


def _katex_font_faces() -> str:
    """@font-face declarations for the vendored KaTeX fonts.

    The vendored katex.css intentionally drops KaTeX's own @font-face
    blocks (they reference woff2/woff URLs that are not bundled); the
    print stylesheet rebuilds them from the ttf files WeasyPrint can load.
    """
    faces = []
    for font_file in sorted((_KATEX_DIR / "fonts").glob("*.ttf")):
        name = font_file.stem  # e.g. KaTeX_Main-BoldItalic
        family = name[: name.rfind("-")]
        variant = name[name.rfind("-") + 1 :]
        weight = "700" if variant in ("Bold", "BoldItalic") else "400"
        style = "italic" if variant in ("Italic", "BoldItalic") else "normal"
        faces.append(
            f"@font-face {{ font-family: '{family}'; font-weight: {weight}; "
            f"font-style: {style}; src: url('{font_file.as_uri()}'); }}"
        )
    return "\n".join(faces)


def _constant_substitution(replacement: str) -> Callable[[re.Match[str]], str]:
    """Build a re.sub replacement callable that always returns one string."""

    def replace(_match: re.Match[str]) -> str:
        return replacement

    return replace


def _is_within(path: Path, root: Path) -> bool:
    """Return whether ``path`` resolves inside the allowed ``root`` directory."""
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _extract_math_spans(text: str) -> tuple[str, dict[str, tuple[str, bool]]]:
    """Swap dollar-delimited formulas for unique ASCII tokens.

    The tokenized text passes through Markdown and sanitization untouched;
    identical (formula, display) pairs share one token so they render once.
    """
    tokens: dict[tuple[str, bool], str] = {}

    def tokenize(formula: str, *, display: bool) -> str:
        key = (formula, display)
        token = tokens.get(key)
        if token is None:
            token = f"{_MATH_TOKEN_PREFIX}{uuid4().hex[:8]}"
            tokens[key] = token
        return token

    text = _MATH_DISPLAY_PATTERN.sub(
        lambda match: tokenize(match.group("formula"), display=True), text
    )
    text = _MATH_INLINE_PATTERN.sub(
        lambda match: tokenize(match.group("formula"), display=False), text
    )
    return text, {token: key for key, token in tokens.items()}


def _inject_math_html(body_html: str, html_by_token: dict[str, str]) -> str:
    """Replace math tokens with rendered KaTeX HTML.

    Called after ``_sanitize_body_html``: KaTeX's strut ``style``
    attributes must not pass through the sanitizer.  Display tokens own
    their whole paragraph (``normalize_report_math`` puts display math on
    its own line), which is replaced by the centered ``katex-display``
    block; inline tokens are plain text and are swapped in place.
    """
    for token, html in html_by_token.items():
        body_html = re.sub(
            rf"<p>(?P<token>{re.escape(token)})</p>",
            _constant_substitution(html),
            body_html,
        )
    for token, html in html_by_token.items():
        body_html = body_html.replace(token, html)
    return body_html


def _render_math_html(body_html: str, token_map: dict[str, tuple[str, bool]]) -> str:
    """Batch-render extracted formulas and inject their KaTeX HTML."""
    from scholight.survey.katex_render import render_formulas

    tokens = list(token_map)
    formulas: list[tuple[int, str, bool]] = []
    oversized: set[int] = set()
    for index, token in enumerate(tokens):
        formula, display = token_map[token]
        if formula.strip() and len(formula) <= _MATH_MAX_CHARS:
            formulas.append((index, formula, display))
        else:
            oversized.add(index)
    rendered = render_formulas(formulas)
    html_by_token: dict[str, str] = {}
    for index, token in enumerate(tokens):
        formula, display = token_map[token]
        if index in oversized or rendered.get(index) is None:
            delimiter = "$$" if display else "$"
            html_by_token[token] = (
                f'<span class="math-fallback">{escape(f"{delimiter}{formula}{delimiter}")}</span>'
            )
        else:
            html = rendered[index]
            assert html is not None
            html_by_token[token] = html
    return _inject_math_html(body_html, html_by_token)


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
    tokenized, token_map = _extract_math_spans(cleaned_markdown)
    body_html = markdown_lib.markdown(
        tokenized,
        extensions=["tables", "fenced_code"],
        output_format="html5",
    )
    body_html = _FIGURE_CAPTION_PATTERN.sub(
        lambda match: f'{match.group(1)}<p class="chart-caption">{match.group(2)}</p>',
        body_html,
    )
    body_html = _embed_images(body_html, images)
    body_html = _sanitize_body_html(body_html)
    body_html = _render_math_html(body_html, token_map)
    body_html = _drop_leading_duplicate_title(body_html, title)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8" />\n'
        f"<title>{escape(title)}</title>\n"
        f"<style>\n{_font_faces()}\n{_katex_font_faces()}\n"
        f"{_katex_css()}\n{_PRINT_CSS}\n</style>\n"
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
    import logging

    import weasyprint

    logging.getLogger("fontTools").setLevel(logging.ERROR)
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
            resource_path = Path(unquote(parsed.path)).resolve()
            allowed_roots = (_FONTS_DIR.resolve(), (_KATEX_DIR / "fonts").resolve())
            if not any(_is_within(resource_path, root) for root in allowed_roots):
                raise ValueError("PDF resources must be bundled assets")
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
    "fallback_title",
    "render_report_pdf",
]


def fallback_title(initial_request: str) -> str:
    """Derive a short display title from a request's first line."""
    first_line = next((line.strip() for line in initial_request.splitlines() if line.strip()), "")
    if len(first_line) <= 96:
        return first_line or "Untitled survey"
    return f"{first_line[:95].rstrip()}…"
