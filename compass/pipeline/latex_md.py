"""LaTeX source → Markdown conversion pipeline.

Pipeline stages:
  1. Find main .tex file (grep \\documentclass)
  2. Strip comments (line-based %, handles \\% escape)
  3. Flatten \\input{}/\\include{} into single compilable .tex
  4. Normalize pandoc-incompatible environments (revtex4, eqnarray)
  5. Resolve \\bibliography → inline .bbl content
  6. pandoc -f latex+raw_tex -t markdown --standalone --wrap=none

Formula preservation:
  pandoc outputs math in native LaTeX notation ( \\(...\\) / \\[...\\] ),
  so formulas survive the conversion intact.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


class LatexMdError(Exception):
    """LaTeX → Markdown conversion failed at one of the pipeline stages."""


# ── Public API ───────────────────────────────────────────────────────────────


def latex_to_markdown(latex_dir: str | Path) -> str:
    """Convert a LaTeX source directory to markdown.

    Args:
        latex_dir: Path to the unpacked LaTeX source (e.g. ``papers/2024/01/15/2401.12345/latex/``).

    Returns:
        Markdown text with formulas preserved in \\(...\\) / \\[...\\] notation.

    Raises:
        LatexMdError: Any stage failed (no main .tex, flatten error, pandoc crash).
    """
    latex_dir = Path(latex_dir)
    if not latex_dir.is_dir():
        raise FileNotFoundError(f"LaTeX directory not found: {latex_dir}")

    # Stage 1: Find main .tex
    main_tex = _find_main_tex(latex_dir)
    if main_tex is None:
        raise LatexMdError(f"No main .tex file found in {latex_dir} (no \\documentclass)")

    logger.debug("latex_main_found", main_tex=str(main_tex), latex_dir=str(latex_dir))

    # Stage 2: Strip comments
    t0 = time.monotonic()
    stripped = _strip_comments(main_tex.read_text(encoding="utf-8", errors="replace"))
    logger.debug(
        "latex_stage_done",
        stage="strip_comments",
        main_tex=main_tex.name,
        ms=int((time.monotonic() - t0) * 1000),
    )

    # Stage 3: Flatten \input{}/\include{}
    t0 = time.monotonic()
    flattened = _flatten_inputs(stripped, latex_dir)
    logger.debug(
        "latex_stage_done",
        stage="flatten",
        main_tex=main_tex.name,
        ms=int((time.monotonic() - t0) * 1000),
    )

    # Stage 4: Normalize pandoc-incompatible environments (revtex4, eqnarray)
    t0 = time.monotonic()
    normalized = _normalize_compatibility(flattened)
    logger.debug(
        "latex_stage_done",
        stage="normalize",
        main_tex=main_tex.name,
        ms=int((time.monotonic() - t0) * 1000),
    )

    # Stage 5: Resolve bibliography (.bbl)
    t0 = time.monotonic()
    with_bib = _resolve_bibliography(normalized, latex_dir)
    logger.debug(
        "latex_stage_done",
        stage="bibliography",
        main_tex=main_tex.name,
        ms=int((time.monotonic() - t0) * 1000),
    )

    # Stage 6: pandoc conversion
    t0 = time.monotonic()
    md = _run_pandoc(with_bib)
    logger.debug(
        "latex_stage_done",
        stage="pandoc",
        main_tex=main_tex.name,
        ms=int((time.monotonic() - t0) * 1000),
    )

    logger.info(
        "latex_md_done",
        latex_dir=str(latex_dir),
        chars=len(md),
        main_tex=main_tex.name,
    )
    return md


# ── Stage 1: Find main .tex ──────────────────────────────────────────────────


def _find_main_tex(tex_dir: Path) -> Path | None:
    """Locate the main .tex file in a directory.

    Strategy (first match wins):
      1. Grep for ``\\documentclass`` — gold standard (covers ~82-94%).
      2. If exactly one ``.tex`` file exists → assume it's the main (covers ~78%).
    """
    tex_files = sorted(tex_dir.glob("*.tex"))
    if not tex_files:
        return None

    # Gold standard: grep \documentclass
    for f in tex_files:
        try:
            content = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Match \documentclass not preceded by % on the same line
        if re.search(r"^[^%]*\\documentclass", content, re.MULTILINE):
            return f

    # Fallback: single .tex file
    if len(tex_files) == 1:
        return tex_files[0]

    return None


# ── Stage 2: Strip comments ──────────────────────────────────────────────────


# Matches \begin{...} / \end{...} for verbatim-like environments whose
# content (code, output examples) often contains literal % characters
# and must be preserved intact during comment stripping.
_VERBATIM_ENV_RE = re.compile(
    r"""\\(?:begin|end)\{(verbatim|verbatim\*?|lstlisting|minted|Verbatim)\}""",
    re.MULTILINE,
)


def _strip_comments(content: str) -> str:
    """Remove line-based ``%`` comments from LaTeX source.

    A ``%`` preceded by an *even* number of consecutive backslashes
    (counted right-to-left from the ``%``) is a real comment marker.
    A ``%`` preceded by an *odd* number is a literal percent sign
    (``\\%``) and is preserved together with its backslashes.

    Comments inside verbatim/lstlisting/minted environments are preserved
    intact — code examples like ``print("100% done")`` would be corrupted otherwise.
    """
    cleaned: list[str] = []
    in_verbatim = False
    for line in content.split("\n"):
        # Track verbatim-like environments
        vm = _VERBATIM_ENV_RE.search(line)
        if vm:
            tag, env = vm.group(0), vm.group(1)
            if "begin" in tag:
                in_verbatim = True
                cleaned.append(line.rstrip())
                continue
            if "end" in tag:
                in_verbatim = False
                cleaned.append(line.rstrip())
                continue

        if in_verbatim:
            cleaned.append(line.rstrip())
        else:
            idx = _find_comment_start(line)
            if idx >= 0:
                cleaned.append(line[:idx].rstrip())
            else:
                cleaned.append(line.rstrip())
    return "\n".join(cleaned)


def _find_comment_start(line: str) -> int:
    """Return the index of the first unescaped ``%`` *comment* marker, or -1."""
    for i, ch in enumerate(line):
        if ch == "%":
            # Count consecutive backslashes immediately to the left
            backslash_count = 0
            j = i - 1
            while j >= 0 and line[j] == "\\":
                backslash_count += 1
                j -= 1
            # Even → real comment; odd → escaped literal (\\%)
            if backslash_count % 2 == 0:
                return i
    return -1


# ── Stage 3: Flatten \input{} / \include{} ───────────────────────────────────


# Matches \input{path} or \include{path} — captures the path inside braces
_INPUT_RE = re.compile(
    r"""^\s*\\(?:input|include)(?:only)?\s*\{  # \input{ or \include{ or \includeonly{
        ([^}]+)                                    # path (group 1)
    \}\s*$""",
    re.MULTILINE | re.VERBOSE,
)


def _flatten_inputs(
    content: str, tex_dir: Path, _depth: int = 0, _seen: frozenset[str] | None = None
) -> str:
    """Recursively inline ``\\input{}`` and ``\\include{}`` references.

    Skips ``\\includeonly{}`` — those are compilation directives, not file inclusions.

    Args:
        content:  LaTeX source with optional ``\\input`` / ``\\include`` lines.
        tex_dir:  Directory to resolve relative paths against.
        _depth:   Current recursion depth (internal, for cycle detection debug).
        _seen:    Set of canonical paths already visited (prevents infinite recursion).

    Returns:
        Flattened LaTeX source with all inputs inlined.
    """
    if _seen is None:
        _seen = frozenset()

    _MAX_DEPTH = 10  # safety valve — real LaTeX papers rarely nest deeper than 2

    def _replace(m: re.Match[str]) -> str:
        nonlocal _seen
        file_path = m.group(1).strip()

        # expand .tex extension if missing
        if not file_path.endswith(".tex"):
            file_path += ".tex"

        # Resolve relative to tex_dir.  arXiv source tarballs are often
        # flattened on extraction, so try both the subdirectory path and
        # a plain basename.
        resolved = _resolve_input_path(tex_dir, file_path)
        if resolved is None:
            logger.debug("latex_input_missing", ref=file_path, dir=str(tex_dir))
            return m.group(0)  # keep original — pandoc can ignore it

        # Prevent infinite recursion (self-\\input  or  A -> B -> A)
        if resolved in _seen:
            logger.debug("latex_input_cycle", ref=file_path, path=str(resolved))
            return m.group(0)

        if _depth > _MAX_DEPTH:
            logger.warning("latex_input_max_depth", ref=file_path, depth=_depth)
            return m.group(0)

        try:
            sub = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.debug("latex_input_read_error", ref=file_path, path=str(resolved))
            return m.group(0)

        # Recurse into the sub-file
        return _flatten_inputs(sub, tex_dir, _depth=_depth + 1, _seen=_seen | {resolved})

    return _INPUT_RE.sub(_replace, content)


def _resolve_input_path(tex_dir: Path, path: str) -> Path | None:
    """Resolve an ``\\input`` path relative to *tex_dir*.

    Tries multiple strategies because arXiv source extraction flattens
    directory hierarchies:
      1. ``tex_dir / path``  (subdirectory preserved, if any)
      2. ``tex_dir / basename(path)``  (flattened — most common)
    """
    # Strategy 1: resolve relative to tex_dir
    candidate = (tex_dir / path).resolve()
    if candidate.is_file():
        return candidate

    # Strategy 2: extractor flattens subdirectories → try plain basename
    bn = Path(path).name
    candidate = tex_dir / bn
    if candidate.is_file():
        return candidate

    return None


# ── Stage 4: Normalize pandoc-incompatible environments ──────────────────────


_REVTEX_REFS_RE = re.compile(
    r"\\begin\{references\}(.*?)\\end\{references\}",
    re.DOTALL,
)


def _normalize_compatibility(content: str) -> str:
    """Replace LaTeX constructs that pandoc cannot parse.

    Known issue: ``\\begin{references}`` (revtex4 only).  pandoc does
    not recognize this environment name.  Renaming it to the standard
    ``thebibliography`` preserves all ``\\bibitem`` entries unchanged
    while becoming parseable.
    """
    return _REVTEX_REFS_RE.sub(
        r"\\begin{thebibliography}{99}\1\\end{thebibliography}",
        content,
    )


# ── Stage 5: Resolve bibliography ────────────────────────────────────────────


_BIBLIO_RE = re.compile(
    r"^\s*\\bibliography\s*\{[^}]*\}\s*$",
    re.MULTILINE,
)

# \bibliographystyle{...} is a style declaration, NOT a bibliography content
# command.  It must be removed (pandoc ignores it) rather than replaced with
# .bbl content, which would inject garbage.
_BIBLIOSTYLE_RE = re.compile(
    r"^\s*\\bibliographystyle\s*\{[^}]*\}\s*$",
    re.MULTILINE,
)


def _resolve_bibliography(content: str, tex_dir: Path) -> str:
    """Replace ``\\bibliography{...}`` with inline ``.bbl`` content.

    arXiv papers always ship pre-compiled ``.bbl`` files — there is never
    a raw ``.bib`` in the source bundle.  We just read the ``.bbl`` and
    splice it in.

    If no ``.bbl`` is found, the ``\\bibliography`` line is left as-is
    (pandoc will silently drop it during conversion).
    """
    bbl_files = list(tex_dir.glob("*.bbl"))
    if not bbl_files:
        return content

    bbl_content = bbl_files[0].read_text(encoding="utf-8", errors="replace")

    # Strip \bibliographystyle{...} — pandoc ignores it, and it's not content
    content = _BIBLIOSTYLE_RE.sub("", content)

    # Use a function callback rather than template string — otherwise
    # ``.bbl`` content containing ``\\1`` / ``\\2`` patterns would be
    # misinterpreted by ``re.sub`` as backreferences, silently truncating content.
    return _BIBLIO_RE.sub(lambda _m: bbl_content, content)


# ── Stage 6: pandoc conversion ───────────────────────────────────────────────


def _run_pandoc(content: str) -> str:
    """Run pandoc to convert LaTeX → Markdown.

    On non-zero exit: returns partial stdout (better than nothing) unless
    stdout is also empty, in which case raises LatexMdError.
    """
    tmp_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".tex", mode="w", encoding="utf-8", delete=False
        ) as tf:
            tf.write(content)
            tmp_path = tf.name
    except OSError as exc:
        raise LatexMdError(f"failed to write temp file: {exc}") from exc

    try:
        result = subprocess.run(
            [
                "pandoc",
                "-f",
                "latex+raw_tex",
                "-t",
                "markdown",
                "--standalone",
                "--wrap=none",
                tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        Path(tmp_path).unlink(missing_ok=True)
        raise LatexMdError("pandoc timed out after 60s")
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)

    if result.returncode != 0:
        partial = result.stdout.strip()
        if partial:
            logger.warning(
                "pandoc_partial_output",
                chars=len(partial),
                exit_code=result.returncode,
                stderr_last_line=(result.stderr or "").split("\n")[-1][:200],
            )
            return partial
        raise LatexMdError(
            f"pandoc exited {result.returncode}: {(result.stderr or '(no stderr)')[:500]}"
        )

    if result.stderr:
        logger.debug(
            "pandoc_stderr",
            first_line=(result.stderr or "").split("\n")[0][:200],
        )
    return result.stdout
