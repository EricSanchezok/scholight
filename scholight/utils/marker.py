"""Marker → MinerU universal converter (DEPRECATED — archive only).

This module depends on Marker internals and is retained only for historical
scripts in ``scripts/archive/``.  The production pipeline exclusively uses
MinerU via ``scholight.pipeline.parser``.  Do not add new production callers;
consider removing this file once archive scripts are retired.
"""

from __future__ import annotations

import contextlib
import warnings
from typing import TYPE_CHECKING, Any, Final

from marker.schema import BlockTypes

# Deferred deprecation notice: emit on first actual use (inside
# marker_block_to_content), NOT at import time. The module-level warn
# polluted stderr for every process that merely imports scholight.utils
# (tests, CI, linters), even though the converter is only used by archive
# scripts. Deferring to the function keeps the import side-effect-free.
_DEPRECATION_WARNED = False

if TYPE_CHECKING:
    from marker.schema.blocks.base import Block
    from marker.schema.document import Document

# ============================================================
# 类型映射表 — 唯一权威来源
# ============================================================
# Every Marker BlockType maps precisely to one MinerU type;
# None entries denote low-level elements (Line/Span/Char/Page/Document) and are excluded.

BLOCK_TYPE_MAP: Final[dict[BlockTypes, str | None]] = {
    # ── text 组 ──
    BlockTypes.SectionHeader: "text",
    BlockTypes.Text: "text",
    BlockTypes.TextInlineMath: "text",
    BlockTypes.Caption: "text",
    BlockTypes.TableCell: "text",
    BlockTypes.Form: "text",
    BlockTypes.Handwriting: "text",
    # ── image ──
    BlockTypes.Figure: "image",
    BlockTypes.Picture: "image",
    BlockTypes.FigureGroup: "image",
    BlockTypes.PictureGroup: "image",
    # ── table ──
    BlockTypes.Table: "table",
    BlockTypes.TableGroup: "table",
    # ── equation (display only; inline math 已归入 text) ──
    BlockTypes.Equation: "equation",
    # ── code ──
    BlockTypes.Code: "code",
    # ── list ──
    BlockTypes.ListItem: "list",
    BlockTypes.ListGroup: "list",
    # ── header / footer / footnote ──
    BlockTypes.PageHeader: "header",
    BlockTypes.PageFooter: "page_number",
    BlockTypes.Footnote: "page_footnote",
    # ── reference ──
    BlockTypes.Reference: "ref_text",
    # ── approximate mappings (no exact Marker equivalent) ──
    BlockTypes.ComplexRegion: "image",
    BlockTypes.TableOfContents: "text",
    # ── skipped low-level elements ──
    BlockTypes.Line: None,
    BlockTypes.Span: None,
    BlockTypes.Char: None,
    BlockTypes.Page: None,
    BlockTypes.Document: None,
}

# Known gaps: MinerU "aside_text" and "chart" have no direct Marker counterpart.
#   - chart -> ComplexRegion or Figure falls to "image" (best available approximation)
#   - aside_text -> Marker Text block does not distinguish sidebar content


# ============================================================
# 转换函数
# ============================================================


def marker_block_to_content(
    block: Block,
    document: Document,
    page_idx: int = 0,
) -> dict[str, Any] | None:
    """Convert a single Marker Block into a MinerU content_list entry.

    Args:
        block: Marker schema Block object.
        document: Parent Marker Document (required for text extraction via raw_text).
        page_idx: Page number (0-indexed).

    Returns:
        A dict with ``type`` / ``bbox`` / ``page_idx`` on success;
        text-type entries additionally include ``text``;
        section headers additionally include ``text_level``.
        Returns None for low-level blocks that should be skipped.
    """
    global _DEPRECATION_WARNED
    if not _DEPRECATION_WARNED:
        _DEPRECATION_WARNED = True
        warnings.warn(
            "scholight.utils.marker is deprecated and will be removed. "
            "Use scholight.pipeline.parser (MinerU) for production workflows.",
            DeprecationWarning,
            stacklevel=2,
        )

    if block is None or document is None:
        return None

    try:
        block_type = block.block_type
    except AttributeError:
        return None

    if block_type is None:
        return None

    mapped = _lookup_type(block_type)
    if mapped is None:
        return None

    item: dict[str, Any] = {"type": mapped, "page_idx": page_idx}

    # ── bbox ──
    _attach_bbox(item, block)

    # ── 文本 ──
    if mapped in ("text", "code", "ref_text", "list", "equation"):
        _attach_text(item, block, document, block_type)

    return item


# ============================================================
# Internal helpers
# ============================================================


def _lookup_type(block_type: BlockTypes) -> str | None:
    """Table lookup with fallback: unknown types default to 'text'."""
    if block_type in BLOCK_TYPE_MAP:
        return BLOCK_TYPE_MAP[block_type]
    # Conservative: treat unknown BlockTypes as text to avoid data loss
    return "text"


def _attach_bbox(item: dict[str, Any], block: Block) -> None:
    """Safely extract polygon.bbox -> [x0, y0, x1, y1] with 4 floats."""
    with contextlib.suppress(TypeError, ValueError, AttributeError):
        polygon = getattr(block, "polygon", None)
        if polygon is None:
            return
        bbox = getattr(polygon, "bbox", None)
        if bbox is None or len(bbox) != 4:
            return
        item["bbox"] = [round(float(v), 1) for v in bbox]


def _attach_text(
    item: dict[str, Any],
    block: Block,
    document: Document,
    block_type: BlockTypes,
) -> None:
    """Safely extract block text content and optional heading_level."""
    # Prefer raw_text(document)
    text = ""
    with contextlib.suppress(Exception):
        text = block.raw_text(document)

    if text:
        text = text.strip()
    else:
        # Fallback: try the .text attribute
        raw = getattr(block, "text", None)
        if raw is not None:
            text = str(raw).strip()

    if text:
        item["text"] = text

    # SectionHeader gets heading_level as an extra field
    if block_type == BlockTypes.SectionHeader:
        level = getattr(block, "heading_level", None)
        if level is not None:
            with contextlib.suppress(TypeError, ValueError):
                item["text_level"] = int(level)
