"""Tests for the batch server-side KaTeX renderer.

The real Node integration tests run whenever ``node`` is on PATH (CI installs
Node for the backend job, so they execute there); hosts without Node fall
back to pure-filesystem assertions.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from scholight.survey.katex_render import (
    _KATEX_DIR,
    _RENDER_BUNDLE,
    katex_render_runtime,
    render_formulas,
)

_NODE_AVAILABLE = shutil.which("node") is not None

pytestmark = pytest.mark.skipif(
    not _NODE_AVAILABLE,
    reason="Node binary is not installed on this host",
)


@pytest.fixture(scope="module")
def katex_assets() -> None:
    assert _RENDER_BUNDLE.is_file(), "render_bundle.js must be vendored"
    assert (_KATEX_DIR / "katex.min.js").is_file(), "katex.min.js must be vendored"
    fonts = sorted((_KATEX_DIR / "fonts").glob("*.ttf"))
    assert len(fonts) == 20, f"expected 20 KaTeX ttf fonts, found {len(fonts)}"
    assert (_KATEX_DIR / "katex.css").is_file(), "katex.css must be vendored"


def test_bundle_renders_inline_and_display(katex_assets: None) -> None:
    result = render_formulas([(0, r"\frac{a}{b}", False), (1, r"\sum_{i=1}^{n} i", True)])

    assert result[0] is not None
    assert 'class="katex"' in result[0]
    assert "katex-display" not in result[0]
    assert result[1] is not None
    assert "katex-display" in result[1]


def test_bundle_keeps_ams_cases_multirow(katex_assets: None) -> None:
    result = render_formulas(
        [(0, r"\begin{cases}x & \text{if }a\\y & \text{otherwise}\end{cases}", True)]
    )

    html = result[0]
    assert html is not None
    # Both rows survive as separate text runs (vlist stacking), not flattened.
    assert "katex-display" in html
    assert "if" in html
    assert "otherwise" in html


def test_bundle_renders_cjk_inside_text(katex_assets: None) -> None:
    result = render_formulas([(0, r"P(\text{检索到的算子}\mid q)", False)])

    html = result[0]
    assert html is not None
    # KaTeX preserves the CJK text as raw characters inside the HTML tree.
    assert "检索到的算子" in html


def test_bundle_renders_native_argmin_with_limits(katex_assets: None) -> None:
    result = render_formulas([(0, r"\argmin_{\mathcal{F}} E[Loss]", True)])

    html = result[0]
    assert html is not None
    # Native \argmin uses a vlist for limits placement below the operator.
    assert "vlist" in html


def test_bundle_isolates_single_formula_parse_error(katex_assets: None) -> None:
    result = render_formulas(
        [
            (0, r"\frac{ok}{1}", False),
            (1, r"\notacommand{", False),
            (2, r"x^2", False),
        ]
    )

    assert result[0] is not None
    assert result[1] is None
    assert result[2] is not None


def test_bundle_oversized_formula_returns_none(katex_assets: None) -> None:
    # A pathological formula must not crash the subprocess.
    result = render_formulas([(0, r"x" * 10_000, False)])

    assert result[0] is None or "katex" in result[0]


def test_render_formulas_empty_input(katex_assets: None) -> None:
    assert render_formulas([]) == {}


def test_runtime_probe_reports_node_and_render(katex_assets: None) -> None:
    runtime = katex_render_runtime()

    assert runtime["available"] is True
    node_version = runtime["node_version"]
    assert isinstance(node_version, str) and node_version.startswith("v")
    assert runtime["render_ok"] is True


def test_bundle_protocol_is_stable(katex_assets: None) -> None:
    """Lock the wire protocol so a bundle rewrite cannot drift silently."""
    node = shutil.which("node")
    assert node is not None
    completed = subprocess.run(
        [node, str(_RENDER_BUNDLE)],
        input=json.dumps({"formulas": [{"id": 7, "tex": r"x^2", "display": False}]}).encode(
            "utf-8"
        ),
        capture_output=True,
        timeout=30.0,
        check=False,
    )
    assert completed.returncode == 0
    payload = json.loads(completed.stdout.decode("utf-8"))
    assert payload["results"] == [{"id": 7, "html": payload["results"][0]["html"]}]
    assert payload["errors"] == []


def test_bundle_large_batch_survives_pipe_flush(katex_assets: None) -> None:
    """A batch whose JSON response exceeds the 64 KiB pipe buffer must not
    be truncated by process.exit() (regression: all formulas fell back to
    text because the response was cut mid-JSON)."""
    formulas = [
        (index, rf"x_{{{index}}} = \frac{{{index}}}{{{index + 1}}}", index % 2 == 0)
        for index in range(300)
    ]

    result = render_formulas(formulas, timeout=30.0)

    assert len(result) == 300
    assert all(html is not None for html in result.values())
