"""PDF → Markdown — two backends for different speed/quality tradeoffs.

Default (``fast=False``): pymupdf4llm, tuned for text-first extraction with
image/graphic rendering disabled.  Best for small-scale / daily incremental
ingestion where layout-aware markdown headings are valuable.

Fast (``fast=True``): pymupdf ``get_text("text")`` direct page extraction.
No layout analysis — skips TOC detection, table recognition, font clustering,
and cross-page text merging.  Each page renders as ``## Page N`` markdown
block.  ~300x faster than the RAG pipeline.  Use for bulk/batch processing
(300M+ papers).

Both paths produce valid markdown suitable for downstream embedding/chunking.
"""

from __future__ import annotations

from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


class PDFMdError(Exception):
    """PDF → Markdown conversion failed."""


# ── Public API ─────────────────────────────────────────────────────────────────


def pdf_to_markdown(pdf_path: str | Path, *, fast: bool = False) -> str:
    """Convert a single PDF to markdown.

    Two backends:

    - ``fast=False`` (default): pymupdf4llm RAG pipeline — layout-aware,
      preserves heading hierarchy.  ~5-40 s per paper.

    - ``fast=True``: pymupdf ``get_text("text")`` — raw text extraction per
      page, output as ``## Page N`` blocks.  ~0.02-0.2 s per paper (~300x).

    Args:
        pdf_path: Path to the PDF file.
        fast: If True, use the direct text extraction backend.

    Returns:
        Markdown text extracted from the PDF.

    Raises:
        FileNotFoundError: *pdf_path* does not exist.
        PDFMdError: Conversion failed.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    try:
        md = _call_pymupdf_fast(pdf_path) if fast else _call_pymupdf4llm(pdf_path)
    except PDFMdError:
        raise
    except Exception as exc:
        raise PDFMdError(f"Conversion failed on {pdf_path}: {exc}") from exc

    logger.debug("pdf_md_done", path=str(pdf_path), chars=len(md), fast=fast)
    return md


# ── Internals ─────────────────────────────────────────────────────────────────


def _call_pymupdf4llm(pdf_path: Path) -> str:
    """pymupdf4llm RAG pipeline — layout-aware, slower."""
    import pymupdf4llm

    return pymupdf4llm.to_markdown(
        str(pdf_path),
        ignore_images=True,
        ignore_graphics=True,
        write_images=False,
        page_chunks=False,
        show_progress=False,
        table_strategy="lines_strict",
    )


def _call_pymupdf_fast(pdf_path: Path) -> str:
    """pymupdf direct text extraction — fast, no layout analysis.

    Produces ``## Page N`` sections for downstream chunking.
    """
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        parts: list[str] = []
        for i, page in enumerate(doc):
            text = page.get_text("text")
            if text.strip():
                parts.append(f"## Page {i + 1}\n\n{text}")
        return "\n\n".join(parts)
    finally:
        doc.close()
