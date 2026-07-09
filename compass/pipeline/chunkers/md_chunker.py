"""Markdown chunker — recursive character split for plain-Markdown text.

Unlike ``content_list_chunker`` (which needs structured ``content_list``
with ``text_level`` fields), this module operates on **raw Markdown strings**:

- ``latex.md`` from pandoc (clean ATX headings, LaTeX math intact)
- ``pdf.md`` from pymupdf4llm (messy headings, math replaced by picture placeholders)

Strategy (backed by literature):
    Recursive Character Split (512-token / ~1500-char target) is the most
    reliable chunking method for academic papers when structural metadata is
    absent.  Vecta's 2026 benchmark (50 papers) placed it at 69% accuracy vs
    54% for semantic chunking.  We keep the force-split + merge post-processing
    from the existing content_list chunker.

References:
    - Vecta (Feb 2026): "We benchmarked 7 chunking strategies — most advice was wrong"
    - NAACL 2025 Findings: semantic chunking costs not justified by consistent gains
    - S2 Chunking (arXiv 2025): spatial+semantic hybrid needs bounding box data
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from compass.pipeline.chunkers._utils import _force_split_text

TARGET_CHARS = 2500  # ~ 830 tokens (roughly 3 chars/token for English)
OVERLAP_CHARS = 0  # academic papers are well-structured at paragraph boundaries; overlap adds noise
MERGE_MIN = (
    500  # merge adjacent chunks shorter than this (below ~150 tokens, too small for retrieval)
)
TAIL_MIN = 500  # minimum chars for standalone chunk — smaller ones get merged into predecessor


@dataclass
class MdChunk:
    content: str
    chunk_index: int


# ── Preprocessing patterns ─────────────────────────────────────────────────

# pymupdf4llm picture placeholder
_PICTURE_PLACEHOLDER = re.compile(r"\*\*==> picture \[.*?\] intentionally omitted <==\*\*")
# pymupdf4llm picture text block (caption extraction artifacts)
_PICTURE_TEXT_BLOCK = re.compile(
    r"\*\*----- Start of picture text -----\*\*.*?\*\*----- End of picture text -----\*\*",
    re.DOTALL,
)
# Isolated page numbers: a line that is ONLY digits (1-3 digits)
_PAGE_NUMBER = re.compile(r"^\d{1,3}$", re.MULTILINE)
# YAML frontmatter block (pandoc-style)
_RE_YAML_BLOCK = re.compile(r"^---\n(.*?)^---", re.DOTALL | re.MULTILINE)
# YAML title field (supports `title: ...` and `title: |\n  ...`)
_RE_YAML_TITLE = re.compile(
    r"^title:\s*(?:[|>].*?\n)?(.*?)(?:^author:|^---)", re.DOTALL | re.MULTILINE
)
# YAML author list (YAML list items: `- Name` or `- Name$^{1}$`)
_RE_YAML_AUTHOR = re.compile(r"^\s*-\s+(.+)$", re.MULTILINE)
# Standalone LaTeX commands without braces: \maketitle, \clearpage, etc.
_LATEX_STANDALONE = re.compile(r"^\\(?:maketitle|clearpage|newpage|pagebreak)\s*$", re.MULTILINE)
# LaTeX noise blocks we want to drop
_LATEX_NOISE_BRACED = re.compile(
    r"\\(?:email|keywords|pacs|preprint)\{[^}]*\}",
    re.DOTALL,
)
# \affiliation{...} — keep the content but strip LaTeX formatting
_RE_AFFILIATION = re.compile(r"\\affiliation\{([^}]*)\}", re.DOTALL)
# `\ref{tab:foo}` / `\ref{fig:bar}` → [foo]
_LATEX_REF = re.compile(r"`\\ref\{(.+?)\}`\{=latex\}")
# `\href{url}{text}`{=latex} → text [url]
_LATEX_HREF = re.compile(r"`\\href\{(.+?)\}\{(.+?)\}`\{=latex\}")
# Collapse repeated blank lines
_MULTI_BLANK = re.compile(r"\n{3,}")


# ── Public API ─────────────────────────────────────────────────────────────


def chunk_markdown(text: str, *, source: str = "latex") -> list[MdChunk]:
    """Split Markdown text into ~1500-char chunks via recursive character splitting.

    Args:
        text: Raw Markdown string (from latex_to_markdown or pdf_to_markdown).
        source: ``"latex"`` or ``"pdf"`` — controls preprocessing filters.

    Returns:
        List of MdChunk ordered by position in the document.

    Raises:
        ValueError: if *source* is not ``"latex"`` or ``"pdf"``.
    """
    if source not in _VALID_SOURCES:
        raise ValueError(f"source must be 'latex' or 'pdf', got {source!r}")
    text = _preprocess(text, source)
    # Phase 1: recursive character split
    raw = _recursive_split(text, TARGET_CHARS, OVERLAP_CHARS)
    # Phase 2: force-split oversized single-paragraph chunks (reused from chunker.py)
    i = 0
    while i < len(raw):
        if len(raw[i]) > TARGET_CHARS * 2:
            parts = _force_split_text(raw[i])
            raw[i : i + 1] = parts
            i += len(parts)
        else:
            i += 1
    # Phase 3: merge adjacent mini-chunks
    merged: list[str] = []
    i = 0
    while i < len(raw):
        if len(raw[i]) >= MERGE_MIN or i + 1 >= len(raw):
            merged.append(raw[i])
            i += 1
        else:
            merged.append((raw[i] + "\n\n" + raw[i + 1]).strip())
            i += 2
    # Phase 4: merge tail orphans — any chunk < TAIL_MIN gets absorbed into neighbor
    # (post-merge can leave small trailing fragments; sweep them up)
    i = 0
    while i < len(merged):
        if len(merged[i]) < TAIL_MIN:
            if i > 0:
                # Merge into previous chunk (prefer backward — doesn't shift subsequent indices)
                merged[i - 1] = (merged[i - 1] + "\n\n" + merged[i]).strip()
                merged.pop(i)
            elif len(merged) > 1:
                # First chunk is small but not last — merge forward
                merged[i + 1] = (merged[i] + "\n\n" + merged[i + 1]).strip()
                merged.pop(i)
            else:
                # Only one chunk in the whole document — keep it as-is
                i += 1
        else:
            i += 1
    # Phase 5: finalise — reject empty chunks
    return [MdChunk(content=c, chunk_index=ci) for ci, c in enumerate(merged) if c.strip()]


# ── Preprocessing ──────────────────────────────────────────────────────────


def _preprocess(text: str, source: str) -> str:
    """Strip markup noise before chunking, preserving title/author/affiliation."""
    # Both sources: collapse 3+ blank lines to 2
    text = _MULTI_BLANK.sub("\n\n", text)

    if source == "latex":
        prefix_parts: list[str] = []

        # Extract title + author from YAML frontmatter before removing it
        fm_match = _RE_YAML_BLOCK.search(text)
        if fm_match:
            fm_text = fm_match.group(1)
            # Title
            title_match = _RE_YAML_TITLE.search(fm_text)
            if title_match:
                t = _yaml_title_text(title_match.group(1))
                if t:
                    prefix_parts.append(t)
            # Authors
            author_names: list[str] = []
            in_author_section = False
            for line in fm_text.split("\n"):
                if line.startswith("author:"):
                    in_author_section = True
                    continue
                if in_author_section:
                    am = _RE_YAML_AUTHOR.match(line)
                    if am:
                        # Strip LaTeX superscripts like $^{1}$
                        name = re.sub(r"\$\^\{[^}]*\}\$", "", am.group(1)).strip()
                        if name:
                            author_names.append(name)
                    elif line and not line.startswith("-") and not line.startswith(" "):
                        break
            if author_names:
                prefix_parts.append(", ".join(author_names))
            # Remove the YAML block
            text = text[fm_match.end() :]

        # Extract affiliation content
        text = _RE_AFFILIATION.sub(_affiliation_text, text)

        # Strip standalone LaTeX commands (\maketitle, \clearpage, etc.)
        text = _LATEX_STANDALONE.sub("", text)
        # Strip noise braced commands (email, keywords, pacs)
        text = _LATEX_NOISE_BRACED.sub("", text)
        # Convert `\ref{...}`{=latex} → [ref-name]
        text = _LATEX_REF.sub(r"[\1]", text)
        # Convert `\href{url}{text}`{=latex} → text [url]
        text = _LATEX_HREF.sub(r"\2 [\1]", text)

        # Prepend title + author to body text
        if prefix_parts:
            text = ("\n\n".join(prefix_parts) + "\n\n" + text).strip()

        return text.strip()

    # pdf source: strip picture placeholders, page numbers (flags compiled into pattern)
    text = _PICTURE_PLACEHOLDER.sub("", text)
    text = _PICTURE_TEXT_BLOCK.sub("", text)
    text = _PAGE_NUMBER.sub("", text)
    return text.strip()


def _yaml_title_text(raw: str) -> str:
    """Clean YAML title field: collapse multi-line, strip backslashes and pipes."""
    # Remove YAML block scalars (|, >)
    raw = raw.strip()
    if raw.startswith("|"):
        # `title: |\n  line1\n  line2` → grab until next YAML key
        raw = raw[1:].strip()
    # Collapse leading whitespace-per-line
    lines = [ln.strip() for ln in raw.split("\n") if not ln.strip().startswith(("author:", "---"))]
    # Join, strip trailing backslashes (LaTeX line breaks)
    return " ".join(lines).rstrip("\\").strip()


def _affiliation_text(match: re.Match[str]) -> str:
    """Extract affiliation content, stripping LaTeX line continuations."""
    raw = match.group(1)
    # Replace \\ with spaces, collapse whitespace
    return re.sub(r"\s+", " ", raw.replace("\\\\", " ")).strip()


# ── Recursive character split ──────────────────────────────────────────────


def _recursive_split(
    text: str,
    chunk_size: int = TARGET_CHARS,
    overlap: int = OVERLAP_CHARS,
) -> list[str]:
    """Recursively split at natural boundaries (paragraph → line → sentence → word → char).

    Overlap is applied only at the outermost level to avoid accumulation across
    recursive calls.
    """
    separators = ["\n\n", "\n", ". ", " ", ""]
    raw = _split_no_overlap(text, separators, chunk_size)
    if not raw or overlap <= 0 or len(raw) <= 1:
        return raw
    return _apply_overlap(raw, overlap)


def _split_no_overlap(
    text: str,
    separators: list[str],
    chunk_size: int,
) -> list[str]:
    """Core recursive split without overlap — see _recursive_split for public API."""
    chunks: list[str] = []
    # Base case
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    # Try each separator from coarsest to finest
    for sep in separators:
        if sep == "":
            return _split_character(text, chunk_size)
        parts = text.split(sep)
        if len(parts) > 1:
            break
    else:
        return _split_character(text, chunk_size)

    # Walk through parts, accumulating until chunk_size
    buf: list[str] = []
    buf_len = 0
    for part in parts:
        plen = len(part) + (len(sep) if buf else 0)
        if buf and buf_len + plen > chunk_size:
            chunk_text = sep.join(buf)
            buf_chunks = _split_no_overlap(chunk_text, separators[1:], chunk_size)
            chunks.extend(buf_chunks)
            buf = [part]
            buf_len = len(part)
        else:
            buf.append(part)
            buf_len += plen

    if buf:
        chunk_text = sep.join(buf)
        buf_chunks = _split_no_overlap(chunk_text, separators[1:], chunk_size)
        chunks.extend(buf_chunks)

    return chunks


def _split_character(text: str, chunk_size: int) -> list[str]:
    """Split by characters — last resort when no separator works."""
    chunks: list[str] = []
    n = len(text)
    while len(chunks) * chunk_size < n:
        start = len(chunks) * chunk_size
        end = min(start + chunk_size, n)
        chunk = text[start:end]
        if chunk.strip():
            chunks.append(chunk)
    return chunks


def _apply_overlap(chunks: list[str], overlap: int) -> list[str]:
    """Apply sliding-window overlap (only called once, at the outermost level)."""
    result: list[str] = [chunks[0]]
    for i in range(1, len(chunks)):
        prev = result[-1]
        overlap_text = prev[-overlap:] if len(prev) > overlap else prev
        result.append(overlap_text + "\n" + chunks[i])
    return result


# ── Source validation ──────────────────────────────────────────────────────


_VALID_SOURCES = frozenset({"latex", "pdf"})
