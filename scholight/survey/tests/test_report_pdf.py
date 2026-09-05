"""Survey report PDF rendering contract tests."""

from __future__ import annotations

import base64
from datetime import date
from pathlib import Path

import pytest

from scholight.survey.report_pdf import (
    ReportPdfError,
    build_report_html,
    render_report_pdf,
    render_report_pdf_to_file,
)

GENERATED_ON = date(2026, 8, 29)


@pytest.fixture(autouse=True)
def _fake_katex(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic KaTeX renderer for pipeline tests.

    Real Node/KaTeX integration is covered by test_katex_render.py; here the
    renderer is faked so token extraction, injection order, and fallback
    behavior are exercised without a Node binary.
    """

    def fake_render(formulas: list[tuple[int, str, bool]], **kwargs: object) -> dict[int, str]:
        return {
            formula_id: (
                f'<span class="katex-display"><span class="katex">{tex}</span></span>'
                if display
                else f'<span class="katex">{tex}</span>'
            )
            for formula_id, tex, display in formulas
        }

    monkeypatch.setattr("scholight.survey.katex_render.render_formulas", fake_render)


def test_markdown_renders_gfm_tables_and_fenced_code() -> None:
    html = build_report_html(
        title="Efficient inference",
        markdown_text=(
            "| Method | Score |\n"
            "| --- | --- |\n"
            "| Sparse attention | 0.82 |\n"
            "\n"
            "```python\n"
            "print('hello')\n"
            "```\n"
        ),
        images={},
        generated_on=GENERATED_ON,
    )
    assert "<table>" in html
    assert "<th>Method</th>" in html
    assert '<code class="language-python">' in html
    assert "print('hello')" in html


def test_markdown_renders_math_formulas_as_katex_html(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered = {
        0: '<span class="katex"><span class="katex-html" aria-hidden="true">'
        '<span class="base"><span class="strut" style="height:0.8141em;"></span>'
        '<span class="mord mathnormal">x</span><span class="mord mtight">2</span>'
        "</span></span></span>",
        1: '<span class="katex-display"><span class="katex"><span class="katex-html" '
        'aria-hidden="true"><span class="base"><span class="strut" '
        'style="height:1.2em;vertical-align:-0.35em;"></span><span class="mord">'
        "mathcal</span></span></span></span></span>",
    }
    monkeypatch.setattr(
        "scholight.survey.katex_render.render_formulas",
        lambda formulas, **kwargs: {
            formula_id: rendered[formula_id] for formula_id, _tex, _ in formulas
        },
    )

    html = build_report_html(
        title="Formula report",
        markdown_text="Inline $x^2$ and display:\n\n$$\\mathcal{L}(\\theta)$$\n",
        images={},
        generated_on=GENERATED_ON,
    )

    assert 'class="katex"' in html
    assert 'class="katex-display"' in html
    assert 'class="math-fallback"' not in html
    assert "$$\\mathcal{L}" not in html
    # KaTeX strut styles survive because injection happens after sanitize.
    assert 'style="height:0.8141em;"' in html


def test_internal_html_comments_are_removed() -> None:
    html = build_report_html(
        title="Survey",
        markdown_text="Intro paragraph.\n\n<!--M4-->\n\nClosing.\n",
        images={},
        generated_on=GENERATED_ON,
    )

    assert "<!--M4-->" not in html
    assert "Closing." in html


def test_unsafe_body_html_is_removed() -> None:
    html = build_report_html(
        title="Survey",
        markdown_text=(
            '<script>alert("x")</script>\n'
            '<style>@import url("https://example.invalid/style.css");</style>\n'
            '<p style="background:url(https://example.invalid/x)">Safe text</p>\n'
        ),
        images={},
        generated_on=GENERATED_ON,
    )

    assert "alert" not in html
    assert "example.invalid" not in html
    assert 'style="background' not in html
    assert "Safe text" in html


def test_cover_contains_wordmark_title_and_date() -> None:
    html = build_report_html(
        title="Reliable model evaluation",
        markdown_text="# Reliable model evaluation\n\nBody.",
        images={},
        generated_on=GENERATED_ON,
    )

    assert 'class="cover-wordmark">scholight</span>' in html
    assert 'class="cover-title">Reliable model evaluation</h1>' in html
    assert "August 29, 2026" in html


def test_relative_images_become_embedded_data_uris() -> None:
    html = build_report_html(
        title="Survey",
        markdown_text=("![Landscape](./08_global_picture.png)\n![Plain](08_picture.jpg)\n"),
        images={
            "08_global_picture.png": b"png-bytes",
            "08_picture.jpg": b"jpg-bytes",
        },
        generated_on=GENERATED_ON,
    )

    assert 'src="data:image/png;base64,cG5nLWJ5dGVz"' in html
    assert 'src="data:image/jpeg;base64,anBnLWJ5dGVz"' in html


def test_unknown_and_remote_images_are_dropped() -> None:
    html = build_report_html(
        title="Survey",
        markdown_text=(
            "![Missing](08_missing.png)\n"
            "![Remote](https://example.invalid/pic.png)\n"
            "![Root](/absolute.png)\n"
        ),
        images={"08_other.png": b"kept"},
        generated_on=GENERATED_ON,
    )

    assert "<img" not in html


def test_file_mode_links_only_referenced_images_below_report_root(tmp_path: Path) -> None:
    referenced = tmp_path / "figures" / "used.png"
    referenced.parent.mkdir()
    referenced.write_bytes(b"used")
    (tmp_path / "unused.png").write_bytes(b"must-not-be-loaded")

    html = build_report_html(
        title="Survey",
        markdown_text=(
            "![Used](figures/used.png)\n"
            "![Missing](figures/missing.png)\n"
            "![Escape](../outside.png)\n"
        ),
        images={},
        image_root=tmp_path,
        generated_on=GENERATED_ON,
    )

    assert referenced.as_uri() in html
    assert "data:image" not in html
    assert "unused.png" not in html
    assert html.count("<img") == 1


def test_leading_title_h1_is_not_duplicated() -> None:
    html = build_report_html(
        title="Reliable model evaluation",
        markdown_text=("# Reliable model evaluation\n\n## Abstract\n\nEvidence-backed summary.\n"),
        images={},
        generated_on=GENERATED_ON,
    )

    assert html.count("<h1") == 1
    assert "<h2" in html


def test_unrelated_leading_h1_is_kept() -> None:
    html = build_report_html(
        title="A different DB title",
        markdown_text="# Original report heading\n\nBody.\n",
        images={},
        generated_on=GENERATED_ON,
    )

    assert html.count("<h1") == 2


def test_leading_title_comparison_unescapes_html_entities() -> None:
    html = build_report_html(
        title="A & B",
        markdown_text="# A & B\n\nBody.\n",
        images={},
        generated_on=GENERATED_ON,
    )

    assert html.count("<h1") == 1


def test_render_report_pdf_maps_missing_native_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> object:
        raise OSError("cannot load library 'libgobject-2.0-0'")

    monkeypatch.setattr("scholight.survey.report_pdf._load_weasyprint", unavailable)

    with pytest.raises(ReportPdfError, match="PDF backend"):
        render_report_pdf(
            title="Survey",
            markdown_text="# Survey\n\nBody.",
            images={},
            generated_on=GENERATED_ON,
        )


def test_weasyprint_backend_is_not_imported_at_module_load() -> None:
    import scholight.survey.report_pdf as module

    assert "weasyprint" not in vars(module)


def _weasyprint_importable() -> bool:
    try:
        import weasyprint  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.filterwarnings("ignore")
@pytest.mark.skipif(not _weasyprint_importable(), reason="WeasyPrint native backend unavailable")
def test_render_report_pdf_returns_pdf_bytes() -> None:
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
        "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )

    pdf = render_report_pdf(
        title="Reliable model evaluation",
        markdown_text=(
            "# Reliable model evaluation\n\n"
            "## Abstract\n\n"
            "An evidence-backed survey.\n\n"
            "![Landscape](08_global_picture.png)\n"
        ),
        images={"08_global_picture.png": png},
        generated_on=GENERATED_ON,
    )

    assert pdf[:5] == b"%PDF-"
    assert len(pdf) > 2000


def test_file_renderer_writes_target_and_uses_disk_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = tmp_path / "08_survey.md"
    output = tmp_path / "08_survey.pdf"
    cache = tmp_path / ".cache"
    report.write_text("# Survey\n\nBody.", encoding="utf-8")
    calls: list[dict[str, object]] = []

    class FakeHTML:
        def __init__(self, **kwargs: object) -> None:
            calls.append(kwargs)

        def write_pdf(self, **kwargs: object) -> None:
            calls.append(kwargs)
            Path(str(kwargs["target"])).write_bytes(b"%PDF-file")

    class FakeWeasyPrint:
        HTML = FakeHTML

        @staticmethod
        def default_url_fetcher(url: str, *args: object, **kwargs: object) -> object:
            return {"url": url}

    monkeypatch.setattr("scholight.survey.report_pdf._load_weasyprint", FakeWeasyPrint)

    render_report_pdf_to_file(
        title="Survey",
        markdown_path=report,
        output_path=output,
        asset_root=tmp_path,
        cache_dir=cache,
        generated_on=GENERATED_ON,
    )

    assert output.read_bytes() == b"%PDF-file"
    assert calls[1]["cache"] == str(cache)
    assert str(calls[1]["target"]).endswith(".08_survey.pdf.tmp")


def test_figure_captions_after_images_get_caption_styling() -> None:
    html = build_report_html(
        title="Charts report",
        markdown_text=(
            "![Share \\(2020\\)](figures/chart-1.png)\n\n*Share \\(2020\\) \\[all\\]*\n"
        ),
        images={"figures/chart-1.png": b"png-bytes"},
        generated_on=GENERATED_ON,
    )

    assert '<p class="chart-caption">Share (2020) [all]</p>' in html
    assert "<em>Share" not in html


def test_renderer_unsupported_math_commands_render_as_images_not_fallback() -> None:
    html = build_report_html(
        title="Math report",
        markdown_text=(
            "$$\\textsc{Verified},\\textbf{(T1)}$$\n\nInline $\\mathds{1}[x]$ and $a \\iff b$.\n"
        ),
        images={},
        generated_on=GENERATED_ON,
    )

    assert 'class="katex-display"' in html
    assert 'class="katex"' in html
    assert 'class="math-fallback"' not in html
    assert "\\textsc" not in html
    assert "\\textbf" not in html
    assert "\\mathds" not in html


def test_single_line_display_math_still_renders_as_display() -> None:
    html = build_report_html(
        title="Layout report",
        markdown_text="Value $$E = mc^2$$ inline-shaped.\n",
        images={},
        generated_on=GENERATED_ON,
    )

    assert 'class="katex-display"' in html
    assert 'class="math-fallback"' not in html


def test_math_fallback_when_renderer_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "scholight.survey.katex_render.render_formulas",
        lambda formulas, **kwargs: {formula_id: None for formula_id, _tex, _ in formulas},
    )

    html = build_report_html(
        title="Fallback report",
        markdown_text="Inline $x^2$ and display:\n\n$$\\mathcal{L}(\\theta)$$\n",
        images={},
        generated_on=GENERATED_ON,
    )

    assert 'class="math-fallback"' in html
    assert "\\mathcal{L}(\\theta)" in html
    assert "\\theta" in html
    assert "MATHTOKEN_" not in html


def test_math_oversized_formula_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[tuple[int, str, bool]] = []

    def fake_render(formulas: list[tuple[int, str, bool]], **kwargs: object) -> dict[int, str]:
        called.extend(formulas)
        return {formula_id: "<katex/>" for formula_id, _tex, _ in formulas}

    monkeypatch.setattr("scholight.survey.katex_render.render_formulas", fake_render)

    html = build_report_html(
        title="Oversized report",
        markdown_text=f"$${'x' * 2500}$$\n",
        images={},
        generated_on=GENERATED_ON,
    )

    assert called == []
    assert 'class="math-fallback"' in html


def test_math_tokens_not_injected_outside_math() -> None:
    html = build_report_html(
        title="Token hygiene",
        markdown_text="No formulas here. $x$ still renders.\n",
        images={},
        generated_on=GENERATED_ON,
    )

    assert "MATHTOKEN_" not in html
    assert 'class="katex"' in html


def test_print_css_declares_cjk_font_fallbacks() -> None:
    html = build_report_html(
        title="中文标题回退",
        markdown_text="正文包含中文段落。\n",
        images={},
        generated_on=GENERATED_ON,
    )

    assert "'Noto Sans CJK SC', sans-serif" in html
    assert "'Noto Serif CJK SC', serif" in html
