"""Batch server-side KaTeX rendering for the Survey report PDF.

The branded PDF is assembled by WeasyPrint, which cannot execute the
JavaScript KaTeX runtime.  Formulas are therefore rendered once by a Node
subprocess that runs a vendored KaTeX bundle, and the produced HTML is
injected into the print document.  Rendering is best-effort: any subprocess
failure maps every formula to ``None`` and the caller falls back to a text
span, so a missing Node binary can never block archiving.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- fixed local Node binary and vendored script only.
from pathlib import Path

__all__ = ["render_formulas", "katex_render_runtime"]

_KATEX_DIR = Path(__file__).resolve().parent / "assets" / "katex"
_RENDER_BUNDLE = _KATEX_DIR / "render_bundle.js"

# Subprocess budget is generous: the renderer is invoked once per report
# inside ``asyncio.to_thread``, so the call blocks a worker thread only.
_RENDER_TIMEOUT_SECONDS = 30.0


def render_formulas(
    formulas: list[tuple[int, str, bool]],
    *,
    timeout: float = _RENDER_TIMEOUT_SECONDS,
) -> dict[int, str | None]:
    """Render LaTeX formulas to KaTeX HTML in one Node subprocess.

    Each entry is ``(formula_id, tex, display)``.  The returned dict maps
    formula ids to KaTeX HTML, or ``None`` when that formula failed to parse.
    A Node runtime failure (binary missing, timeout, non-zero exit, invalid
    response) maps *every* id to ``None``; callers degrade to plain-text
    math spans rather than raising.
    """
    empty: dict[int, str | None] = {formula_id: None for formula_id, _tex, _ in formulas}
    if not formulas:
        return {}
    node = shutil.which("node")
    if node is None or not _RENDER_BUNDLE.is_file():
        return empty
    payload = json.dumps(
        {
            "formulas": [
                {"id": formula_id, "tex": tex, "display": display}
                for formula_id, tex, display in formulas
            ]
        }
    ).encode("utf-8")
    try:
        completed = subprocess.run(  # nosec B603
            [node, str(_RENDER_BUNDLE)],
            input=payload,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return empty
    if completed.returncode != 0:
        return empty
    try:
        response = json.loads(completed.stdout.decode("utf-8"))
        results = {item["id"]: item["html"] for item in response.get("results", [])}
        errors = {item["id"] for item in response.get("errors", [])}
    except (ValueError, KeyError, TypeError):
        return empty
    return {
        formula_id: None if formula_id in errors else results.get(formula_id)
        for formula_id, _tex, _ in formulas
    }


def katex_render_runtime() -> dict[str, str | bool]:
    """Probe the KaTeX render stack for the CLI smoke check.

    Returns a dict describing Node availability and whether the vendored
    bundle can render a minimal formula.  Never raises: the smoke report
    treats an unavailable stack as a warning, not a failure.
    """
    node = shutil.which("node")
    if node is None:
        return {"available": False, "node_version": "", "render_ok": False}
    try:
        version = subprocess.run(  # nosec B603
            [node, "--version"], capture_output=True, timeout=10.0, check=False
        )
        node_version = version.stdout.decode("utf-8").strip()
    except (OSError, subprocess.TimeoutExpired):
        return {"available": True, "node_version": "", "render_ok": False}
    rendered = render_formulas([(0, r"E=mc^2", False)], timeout=10.0)
    return {
        "available": True,
        "node_version": node_version,
        "render_ok": rendered.get(0) is not None,
    }
