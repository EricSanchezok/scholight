"""Survey report PDF rendering contract tests."""

from __future__ import annotations

import base64
from datetime import date

import pytest

from scholight.survey.report_pdf import (
    ReportPdfError,
    build_report_html,
    render_report_pdf,
)

GENERATED_ON = date(2026, 8, 29)


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


def test_markdown_renders_math_formulas_as_self_contained_images() -> None:
    html = build_report_html(
        title="Formula report",
        markdown_text="Inline $x^2$ and display:\n\n$$\\mathcal{L}(\\theta)$$\n",
        images={},
        generated_on=GENERATED_ON,
    )

    assert 'class="math-inline"' in html
    assert 'class="math-display"' in html
    assert 'src="data:image/png;base64,' in html
    assert "$$\\mathcal{L}" not in html


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

    assert 'class="math-display"' in html
    assert 'class="math-inline"' in html
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

    assert 'class="math-display"' in html
    assert 'class="math-fallback"' not in html


def test_math_images_carry_dpi_corrected_pixel_dimensions() -> None:
    html = build_report_html(
        title="Sizing report",
        markdown_text="Value $E = mc^2$ and display:\n\n$$\n\\mathcal{L}(\\theta)\n$$\n",
        images={},
        generated_on=GENERATED_ON,
    )

    import re

    tags = re.findall(r'<img class="math-(?:inline|display)"[^>]*>', html)
    assert tags, "expected rendered math images"
    for tag in tags:
        match = re.search(r'width="(\d+)" height="(\d+)"', tag)
        assert match is not None, f"missing CSS-pixel dimensions: {tag[:120]}"
        width, height = int(match.group(1)), int(match.group(2))
        # dpi-corrected: a 10pt-ish inline glyph must stay far below its raw
        # 180-dpi pixel height (which would be roughly 2x larger)
        assert 1 <= height <= 40, f"unexpected inline height {height}"
        assert width >= 1


def test_print_css_declares_cjk_font_fallbacks() -> None:
    html = build_report_html(
        title="中文标题回退",
        markdown_text="正文包含中文段落。\n",
        images={},
        generated_on=GENERATED_ON,
    )

    assert "'Noto Sans CJK SC', sans-serif" in html
    assert "'Noto Serif CJK SC', serif" in html
