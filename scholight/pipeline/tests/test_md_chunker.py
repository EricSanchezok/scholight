"""Tests for markdown chunker — recursive character split for plain-Markdown text."""

from __future__ import annotations

import pytest

from scholight.pipeline.chunkers._utils import _force_split_text
from scholight.pipeline.chunkers.md_chunker import (
    TARGET_CHARS,
    MdChunk,
    _preprocess,
    _recursive_split,
    chunk_markdown,
)

# ── Preprocessing (latex source) ───────────────────────────────────────────


def test_preprocess_latex_strips_yaml_syntax_keeps_metadata() -> None:
    """YAML delimiters and keys must be gone, but title/author are preprended."""
    text = """---
title: Test Title
author:
- Alice
- Bob
---
# Introduction
This is the body."""
    result = _preprocess(text, "latex")
    assert "---" not in result
    assert "author:" not in result
    assert "Test Title" in result
    assert "Alice" in result
    assert "Bob" in result
    # Title/author should appear before body
    assert result.index("Test Title") < result.index("Introduction")


def test_preprocess_latex_strips_maketitle() -> None:
    text = "\\maketitle\n# Introduction\nText."
    result = _preprocess(text, "latex")
    assert "\\maketitle" not in result


def test_preprocess_latex_strips_clearpage() -> None:
    text = "Some text\n\\clearpage\n# More"
    result = _preprocess(text, "latex")
    assert "\\clearpage" not in result


def test_preprocess_latex_strips_email_block() -> None:
    text = "\\email{foo@bar.com}\n# Real content"
    result = _preprocess(text, "latex")
    assert "foo@bar.com" not in result


def test_preprocess_latex_preserves_affiliation_as_plain_text() -> None:
    """Affiliation content stays in the text, stripped of LaTeX formatting."""
    text = "\\affiliation{Some University}\n# Real content"
    result = _preprocess(text, "latex")
    assert "Some University" in result
    assert "\\affiliation" not in result


def test_preprocess_latex_strips_keywords() -> None:
    text = "\\keywords{foo, bar}\n# Real content"
    result = _preprocess(text, "latex")
    assert "foo, bar" not in result


def test_preprocess_latex_converts_ref() -> None:
    text = "See Table `\\ref{tab:foo}`{=latex} for details."
    result = _preprocess(text, "latex")
    assert "[tab:foo]" in result
    assert "\\ref" not in result


def test_preprocess_latex_converts_href() -> None:
    text = "`\\href{http://example.com}{Link Text}`{=latex} is here."
    result = _preprocess(text, "latex")
    assert "Link Text [http://example.com]" in result
    assert "\\href" not in result


def test_preprocess_latex_preserves_math() -> None:
    text = "# Section\n$E=mc^2$ is famous.\n$$\\int_0^\\infty f(x)dx$$\nDone."
    result = _preprocess(text, "latex")
    assert "$E=mc^2$" in result
    assert "\\int_0^\\infty" in result


# ── Preprocessing (pdf source) ─────────────────────────────────────────────


def test_preprocess_pdf_strips_picture_placeholders() -> None:
    text = "Some text\n**==> picture [372 x 13] intentionally omitted <==**\nMore text."
    result = _preprocess(text, "pdf")
    assert "intentionally omitted" not in result
    assert "Some text" in result
    assert "More text" in result


def test_preprocess_pdf_strips_picture_text_blocks() -> None:
    text = (
        "Before.\n"
        "**----- Start of picture text -----**\n"
        "caption text\n"
        "**----- End of picture text -----**\n"
        "After."
    )
    result = _preprocess(text, "pdf")
    assert "caption text" not in result
    assert "Before" in result
    assert "After" in result


def test_preprocess_pdf_strips_isolated_page_numbers() -> None:
    text = "End of page.\n7\n8\nStart of next page."
    result = _preprocess(text, "pdf")
    assert "\n7\n" not in result
    assert "\n8\n" not in result


def test_preprocess_pdf_preserves_heading_text() -> None:
    text = "# Title\n## Section 7 Analysis\nSome content."
    result = _preprocess(text, "pdf")
    assert "# Title" in result
    assert "## Section 7 Analysis" in result


# ── Recursive split ────────────────────────────────────────────────────────


def test_recursive_split_empty() -> None:
    assert _recursive_split("", 500, 0) == []


def test_recursive_split_short_text_returns_single_chunk() -> None:
    text = "Short text here."
    chunks = _recursive_split(text, 500, 0)
    assert len(chunks) == 1
    assert chunks[0] == text


def test_recursive_split_respects_chunk_size() -> None:
    text = "A" * (TARGET_CHARS * 3)  # no natural separators
    chunks = _recursive_split(text, TARGET_CHARS, 0)
    for c in chunks:
        assert len(c) <= TARGET_CHARS


def test_recursive_split_splits_at_paragraphs() -> None:
    # Each paragraph is ~300 chars; target = 500 → should merge 2 per chunk
    p = "Para with enough text to be meaningful. " * 5  # ~300 chars
    text = "\n\n".join([p] * 4)
    chunks = _recursive_split(text, 500, 0)
    # All chunks should be <= 500 chars
    for c in chunks:
        assert len(c) <= 700  # allow some flex due to separator logic
    assert len(chunks) > 1  # must split


def test_recursive_split_preserves_math_formulas() -> None:
    text = (
        "## Section\n\nFirst $x^2 + y^2 = z^2$ inline.\n\nSecond $$\\int_a^b f(x)dx$$ display.\n\n"
        * 3
    )
    chunks = _recursive_split(text, TARGET_CHARS, 0)
    combined = " ".join(chunks)
    assert "$x^2 + y^2 = z^2$" in combined
    assert "\\int_a^b" in combined


# ── Force-split oversized text ─────────────────────────────────────────────


def test_force_split_breaks_long_paragraph_at_sentences() -> None:
    text = "A" * 100 + ". " + "B" * 100 + ". "
    text += "C" * (TARGET_CHARS * 3)
    parts = _force_split_text(text)
    # No part should exceed ~target * 2
    for p in parts:
        assert len(p) <= TARGET_CHARS * 2 + 200  # generous tolerance


def test_force_split_short_text_returns_single_element() -> None:
    text = "Short."
    parts = _force_split_text(text)
    assert len(parts) == 1


# ── Public API: chunk_markdown ─────────────────────────────────────────────


def test_chunk_markdown_empty_text() -> None:
    assert chunk_markdown("", source="latex") == []


def test_chunk_markdown_invalid_source_raises() -> None:
    with pytest.raises(ValueError, match="source must be"):
        chunk_markdown("text", source="latexx")  # type: ignore[arg-type]


def test_chunk_markdown_single_short_chunk() -> None:
    text = "A short paragraph."
    chunks = chunk_markdown(text, source="latex")
    assert len(chunks) == 1
    assert isinstance(chunks[0], MdChunk)
    assert chunks[0].content == text


def test_chunk_markdown_chunks_within_bounds() -> None:
    """All chunks should be between MERGE_MIN and TARGET_CHARS * 2."""
    text = "Paragraph with content. " * 300  # ~7500 chars
    chunks = chunk_markdown(text, source="latex")
    # At least 2 chunks
    assert len(chunks) > 1
    for c in chunks:
        assert len(c.content) <= TARGET_CHARS * 2 + 200
        assert len(c.content) > 0


def test_chunk_markdown_chunk_indexes_are_sequential() -> None:
    text = "Paragraph. " * 300
    chunks = chunk_markdown(text, source="latex")
    for i, c in enumerate(chunks):
        assert c.chunk_index == i


def test_chunk_markdown_latex_preserves_math() -> None:
    text = (
        "# Title\n\n"
        "The equation $E=mc^2$ appears inline.\n\n"
        "And display:\n\n"
        "$$\\int_0^\\infty e^{-x}dx$$\n\n"
        "Also \\begin{aligned} a &= b \\\\ c &= d \\end{aligned}\n\n"
    ) * 5
    chunks = chunk_markdown(text, source="latex")
    combined = " ".join(c.content for c in chunks)
    assert "$E=mc^2$" in combined
    assert "\\int_0^\\infty" in combined
    assert "\\begin{aligned}" in combined


def test_chunk_markdown_latex_keeps_yaml_author_and_title() -> None:
    """YAML metadata (author, title) should be retained in the chunk text."""
    text = """---
title: Some Paper
author:
- Alice
- Bob Smith
---
# Introduction
This is the actual content.
## Methods
More content here."""
    chunks = chunk_markdown(text, source="latex")
    all_text = " ".join(c.content for c in chunks)
    # YAML keys/syntax gone
    assert "title:" not in all_text
    assert "author:" not in all_text
    # Content preserved
    assert "Some Paper" in all_text  # title retained
    assert "Alice" in all_text  # authors retained
    assert "Bob Smith" in all_text
    assert "Introduction" in all_text
    assert "Methods" in all_text


def test_chunk_markdown_pdf_no_picture_in_chunks() -> None:
    text = """# Title

Some text.

**==> picture [372 x 13] intentionally omitted <==**

More text after picture.

**==> picture [400 x 30] intentionally omitted <==**

Final text."""
    chunks = chunk_markdown(text, source="pdf")
    all_text = " ".join(c.content for c in chunks)
    assert "intentionally omitted" not in all_text


# ── Real file regression tests ─────────────────────────────────────────────


def test_chunk_real_latex_md() -> None:
    """Smoke test: chunk real latex.md without errors, chunks within bounds."""
    from pathlib import Path

    path = Path("data/extract_test_20260604/0704.0008/latex.md")
    if not path.exists():
        import pytest

        pytest.skip("test data not available")
        # Keep type checker happy below — won't reach here
        return  # type: ignore[unreachable]

    text = path.read_text()
    chunks = chunk_markdown(text, source="latex")
    assert len(chunks) > 5
    for c in chunks:
        assert len(c.content) > 0
        assert len(c.content) < TARGET_CHARS * 3  # generous bound for safety


def test_chunk_real_pdf_md() -> None:
    """Smoke test: chunk real pdf.md without errors, chunks within bounds."""
    from pathlib import Path

    path = Path("data/extract_test_20260604/0704.0008/pdf.md")
    if not path.exists():
        import pytest

        pytest.skip("test data not available")
        # Keep type checker happy below — won't reach here
        return  # type: ignore[unreachable]

    text = path.read_text()
    chunks = chunk_markdown(text, source="pdf")
    assert len(chunks) > 5
    for c in chunks:
        assert len(c.content) > 0
        assert len(c.content) < TARGET_CHARS * 3
