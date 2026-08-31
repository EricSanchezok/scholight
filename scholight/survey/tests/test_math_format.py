"""Deterministic math formatting contracts for reader-facing report markdown."""

from __future__ import annotations

from scholight.survey.math_format import normalize_report_math


def test_single_line_display_math_moves_delimiters_to_own_lines() -> None:
    text = "Intro.\n\n$$E = mc^2$$\n\nOutro.\n"

    normalized = normalize_report_math(text)

    assert normalized == "Intro.\n\n$$\nE = mc^2\n$$\n\nOutro.\n"


def test_closing_delimiter_sharing_a_line_moves_to_its_own_line() -> None:
    text = "$$\n\\sigma_{i}=x\\end{cases}$$\n"

    normalized = normalize_report_math(text)

    assert normalized == "$$\n\\sigma_{i}=x\\end{cases}\n$$\n"


def test_opening_delimiter_sharing_a_line_moves_to_its_own_line() -> None:
    text = "$$\\mathcal{L}(\\theta)\n\\sum_i \\ell_i\n$$\n"

    normalized = normalize_report_math(text)

    assert normalized == "$$\n\\mathcal{L}(\\theta)\n\\sum_i \\ell_i\n$$\n"


def test_prose_with_inline_math_is_untouched() -> None:
    text = "The workflow is $s_{t} \\to g_{t}$ in prose.\n"

    assert normalize_report_math(text) == text


def test_renderer_unsupported_commands_are_substituted_inside_math_only() -> None:
    text = (
        "$$\\textsc{Verified},\\textbf{(T1)},\\mathds{1},\\mathbbm{1}$$\n\n"
        "Prose mentions \\textsc{plain} and $a \\iff b$ with $\\big[x\\big]$.\n"
    )

    normalized = normalize_report_math(text)

    assert "\\mathrm{Verified}" in normalized
    assert "\\mathbf{(T1)}" in normalized
    assert "\\mathbf{1}" in normalized
    assert "$a \\Longleftrightarrow b$" in normalized
    assert "$[x]$" in normalized
    assert "Prose mentions \\textsc{plain}" in normalized  # outside math, untouched


def test_size_prefixes_are_stripped_inside_math() -> None:
    text = "$$\\small\\sigma_{i,t+1}=\\begin{cases}\\sigma_{i,t}\\end{cases}$$\n"

    normalized = normalize_report_math(text)

    assert "\\small" not in normalized
    assert normalized.startswith("$$\n\\sigma")


def test_fenced_code_blocks_and_inline_code_are_preserved() -> None:
    text = (
        "```python\n"
        's = "$$x^2$$ and \\textsc{code}"\n'
        "```\n\n"
        "Inline `$$y^2$$` and `$\\textsc{z}$` stay literal.\n"
    )

    normalized = normalize_report_math(text)

    assert "$$x^2$$" in normalized
    assert "\\textsc{code}" in normalized
    assert "`$$y^2$$`" in normalized
    assert "$\\textsc{z}$" in normalized


def test_normalization_is_idempotent() -> None:
    text = "Intro with $a \\iff b$.\n\n$$\\textsc{V}\\big[x\\big]$$\n\n$$\n\\small\\sigma = 1\n$$\n"

    once = normalize_report_math(text)
    twice = normalize_report_math(once)

    assert once == twice


def test_textrm_with_spacing_command_becomes_text() -> None:
    # Observed in production: \textrm{for \;}i — mathtext fails on \textrm.
    text = "$Y=\\mu,\\quad\\textrm{for \\;}i=1$"

    normalized = normalize_report_math(text)

    assert "\\textrm" not in normalized
    assert "\\text{for \\;}i" in normalized


def test_mathtext_aliases_substituted_inside_math_only() -> None:
    text = (
        "$$\\argmin_{\\mathcal{F}} \\quad \\bm{g} \\quad \\emph{ToGrow}$$\n\n"
        "Prose keeps \\argmin and \\bm literal.\n"
    )

    normalized = normalize_report_math(text)

    assert "\\operatorname{argmin}" in normalized
    assert "\\mathbf{g}" in normalized
    assert "\\mathit{ToGrow}" in normalized
    assert "Prose keeps \\argmin and \\bm literal." in normalized
