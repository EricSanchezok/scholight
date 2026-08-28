"""Survey report PDF rendering contract tests."""

from __future__ import annotations

from datetime import date

import pytest

from scholight.api.report_pdf import (
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


def test_internal_html_comments_are_removed() -> None:
    html = build_report_html(
        title="Survey",
        markdown_text="Intro paragraph.\n\n<!--M4-->\n\nClosing.\n",
        images={},
        generated_on=GENERATED_ON,
    )

    assert "<!--M4-->" not in html
    assert "Closing." in html


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


def test_render_report_pdf_maps_missing_native_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable() -> object:
        raise OSError("cannot load library 'libgobject-2.0-0'")

    monkeypatch.setattr("scholight.api.report_pdf._load_weasyprint", unavailable)

    with pytest.raises(ReportPdfError, match="PDF backend"):
        render_report_pdf(
            title="Survey",
            markdown_text="# Survey\n\nBody.",
            images={},
            generated_on=GENERATED_ON,
        )


def test_weasyprint_backend_is_not_imported_at_module_load() -> None:
    import scholight.api.report_pdf as module

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
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d49444154789c63f8cfc0f01f0005050201edb53a"
        "a60000000049454e44ae426082"
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
