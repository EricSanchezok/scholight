"""MinerU PDF parser — PDF → content_list (primary) + derived markdown.

``content_list`` is the gold source (per-page structured JSON from MinerU).
Markdown is a convenience derivation — chunking, embedding, and retrieval
all operate on content_list.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from scholight.config import settings

if TYPE_CHECKING:
    pass

logger = structlog.get_logger(__name__)


class MinerUParseError(Exception):
    """MinerU parsing failed."""


class MinerUTimeoutError(MinerUParseError):
    """MinerU parsing timed out."""


# ── Public API ─────────────────────────────────────────────────────────


def parse_pdf(
    pdf_path: str | Path,
    output_dir: str | Path,
    *,
    api_key: str | None = None,
    arxiv_id: str | None = None,
) -> Path:
    """Parse a PDF with MinerU, saving ``content_list.json`` as primary output.

    Returns the path to ``{output_dir}/{paper_id}_content_list.json``.

    Markdown is NOT saved by default — it can be regenerated from content_list
    if needed via :func:`content_list_to_markdown`.

    Args:
        pdf_path: Path to the PDF file.
        output_dir: Directory for output files.
        api_key: MinerU API key.  Defaults to ``SCHOLIGHT_MINERU_API_KEY``.
        arxiv_id: Output filename stem.  Defaults to ``pdf_path.stem``.

    Returns:
        Path to the generated ``content_list.json``.

    Raises:
        FileNotFoundError: *pdf_path* does not exist.
        MinerUParseError: The API returned an error.
        MinerUTimeoutError: Task timed out.
        MinerURejectError: File rejected (too large, too many pages).
    """
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    token = api_key or settings.mineru_api_key

    if not token:
        raise MinerUParseError(
            "MinerU API key is required — set SCHOLIGHT_MINERU_API_KEY or pass api_key="
        )
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    paper_id = arxiv_id or pdf_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    result = _call_mineru(pdf_path, token)

    if result.state == "rejected":
        raise MinerURejectError(f"MinerU rejected {pdf_path}: {result.error or 'unknown reason'}")
    if result.err_code:
        raise MinerUParseError(f"MinerU error: {result.err_code} — {result.error}")

    content_list = result.content_list or []
    cl_path = output_dir / f"{paper_id}_content_list.json"
    cl_path.write_text(json.dumps(content_list, ensure_ascii=False, indent=2), encoding="utf-8")

    logger.info(
        "MinerU parse complete",
        paper_id=paper_id,
        cl_items=len(content_list),
    )
    return cl_path


def content_list_to_markdown(content_list: list[dict[str, Any]]) -> str:
    """Regenerate markdown from MinerU content_list.

    This reproduces MinerU's markdown output format.  Useful when markdown is
    needed for human review but we want to avoid storing it alongside the gold
    content_list source.
    """
    lines: list[str] = []
    for item in content_list:
        t = item.get("type", "")
        text = _text(item)

        if t == "text" and item.get("text_level") == 1:
            lines.append(f"## {text}")
        elif t == "text":
            lines.append(text)
        elif t == "equation":
            latex = _str_or_join(item.get("latex", "")) or text
            if latex:
                lines.append(latex)
        elif t == "image":
            img = item.get("img_path", "")
            caption = _str_or_join(item.get("image_caption", ""))
            if img:
                lines.append(f"![]({img})")
            if caption:
                lines.append(caption)
        elif t == "table":
            caption = _str_or_join(item.get("table_caption", ""))
            body = item.get("table_body", "")
            if caption:
                lines.append(caption)
            if body:
                lines.append(body)
        elif t == "code":
            body = item.get("code_body", "")
            if body:
                lines.append(f"```\n{body}\n```")
        elif t in ("list",):
            items = item.get("list_items", [])
            for li in items:
                lines.append(f"- {li}")
        else:
            if text:
                lines.append(text)
    return "\n\n".join(lines)


# ── Internals ─────────────────────────────────────────────────────────


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    retry=retry_if_exception_type((OSError, ConnectionError, RuntimeError)),
)
def _call_mineru(pdf_path: Path, api_token: str) -> MinerUResult:
    from mineru import MinerU

    client = MinerU(api_token)
    return client.extract(str(pdf_path))  # type: ignore[no-any-return]


def _text(item: dict[str, Any]) -> str:
    return _str_or_join(item.get("text", ""))


def _str_or_join(val: object) -> str:
    if isinstance(val, list):
        return " ".join(str(v) for v in val)
    return str(val) if val else ""


class MinerURejectError(MinerUParseError):
    """MinerU rejected the file (size / page limit)."""


# Lightweight type stub for MinerU result (avoids importing mineru at module level)
class MinerUResult:
    content_list: list[dict[str, Any]]
    markdown: str
    err_code: str
    error: str | None
    state: str
