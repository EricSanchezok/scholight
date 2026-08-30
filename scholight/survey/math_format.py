"""Deterministic math formatting for reader-facing Survey markdown.

The report is consumed by two renderers with different dialect support:
the web renders KaTeX through remark-math, and the branded PDF renders
matplotlib mathtext.  Model-written formulas regularly put display math
on a single line (which remark-math then renders inline, uncentered) and
use text-mode commands that only one renderer supports (``\\textsc`` fails
in KaTeX; ``\\textbf``, ``\\big``, ``\\mathds``, ``\\iff`` fail in mathtext).

This module normalizes both concerns with pure text transforms so the
finalized markdown renders correctly on both ends without a model call.
Layout rewrites only touch display blocks that occupy whole lines; prose,
code, and in-text dollar spans keep their shape.
"""

from __future__ import annotations

import re

__all__ = ["normalize_report_math"]

_DISPLAY_LINE = "$$"

# Commands rewritten to equivalents both renderers support.  The negative
# lookahead keeps longer command names (e.g. \textscshape) from matching.
_MATH_COMMAND_SUBSTITUTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\\textsc(?![a-zA-Z])"), r"\\mathrm"),
    (re.compile(r"\\textbf(?![a-zA-Z])"), r"\\mathbf"),
    (re.compile(r"\\textit(?![a-zA-Z])"), r"\\mathit"),
    (re.compile(r"\\textrm(?![a-zA-Z])"), r"\\text"),
    (re.compile(r"\\mathds(?![a-zA-Z])"), r"\\mathbf"),
    (re.compile(r"\\mathbbm(?![a-zA-Z])"), r"\\mathbf"),
    (re.compile(r"\\iff(?![a-zA-Z])"), r"\\Longleftrightarrow"),
    # Size/flow prefixes exist only in full LaTeX; both renderers fail on
    # them, so they are dropped together with one trailing space or tab.
    (
        re.compile(
            r"\\(?:small|normalsize|large|Large|LARGE|huge|Huge|displaystyle|"
            r"scriptstyle|scriptscriptstyle)(?![a-zA-Z])[ \t]?"
        ),
        "",
    ),
    # Manual delimiter sizing: \big( and friends are unsupported by
    # mathtext; the bare delimiter renders fine on both ends.
    (
        re.compile(
            r"\\(?:big|Big|bigl|bigr|bigm|Bigl|Bigr|Bigm|biggl|biggr|biggm|"
            r"Biggl|Biggr|Biggm)(?![a-zA-Z])[ \t]?"
        ),
        "",
    ),
)

_DISPLAY_MATH_PATTERN = re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", re.DOTALL)
_INLINE_MATH_PATTERN = re.compile(r"(?<!\\)\$(?!\$)([^$\n]+?)(?<!\\)\$")
_FENCED_BLOCK_PATTERN = re.compile(r"(```.*?```)", re.DOTALL)
_INLINE_CODE_PATTERN = re.compile(r"(`+[^`\n]*`+)")


def _normalize_display_layout(text: str) -> str:
    """Move whole-line display delimiters onto their own lines.

    ``$$x$$`` on one line and blocks whose opening or closing ``$$``
    shares a line with content are rewritten so every ``$$`` sits alone,
    which is what remark-math needs to produce a centered display block.
    Fenced code blocks pass through untouched.
    """
    output: list[str] = []
    in_code = False
    in_math = False
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("```") and not in_math:
            in_code = not in_code
            output.append(line)
            continue
        if in_code:
            output.append(line)
            continue
        if in_math:
            if stripped == _DISPLAY_LINE:
                in_math = False
                output.append(line)
            elif stripped.endswith(_DISPLAY_LINE) and not stripped.startswith(_DISPLAY_LINE):
                output.append(stripped[:-2].rstrip())
                output.append(_DISPLAY_LINE)
                in_math = False
            else:
                output.append(line)
            continue
        if stripped == _DISPLAY_LINE:
            in_math = True
            output.append(line)
            continue
        if (
            stripped.startswith(_DISPLAY_LINE)
            and stripped.endswith(_DISPLAY_LINE)
            and len(stripped) > 4
        ):
            output.append(_DISPLAY_LINE)
            output.append(stripped[2:-2].strip())
            output.append(_DISPLAY_LINE)
            continue
        if stripped.startswith(_DISPLAY_LINE) and len(stripped) > 2:
            output.append(_DISPLAY_LINE)
            output.append(stripped[2:].strip())
            in_math = True
            continue
        output.append(line)
    return "\n".join(output)


def _substitute_math_commands(text: str) -> str:
    """Apply the command table inside dollar-delimited math spans only."""

    def apply(formula: str) -> str:
        for pattern, replacement in _MATH_COMMAND_SUBSTITUTIONS:
            formula = pattern.sub(replacement, formula)
        return formula

    text = _DISPLAY_MATH_PATTERN.sub(lambda match: f"$${apply(match.group(1))}$$", text)
    return _INLINE_MATH_PATTERN.sub(lambda match: f"${apply(match.group(1))}$", text)


def _normalize_segment(segment: str) -> str:
    parts = _INLINE_CODE_PATTERN.split(segment)
    for index, part in enumerate(parts):
        if index % 2 == 0:
            parts[index] = _substitute_math_commands(_normalize_display_layout(part))
    return "".join(parts)


def normalize_report_math(text: str) -> str:
    """Normalize display-math layout and renderer-unsupported commands.

    Fenced code blocks and inline code spans are preserved verbatim; the
    transform is idempotent so finalization and PDF rendering can both
    apply it safely.
    """
    parts = _FENCED_BLOCK_PATTERN.split(text)
    for index, part in enumerate(parts):
        if index % 2 == 0:
            parts[index] = _normalize_segment(part)
    return "".join(parts)
