"""Deterministic Survey final assembly contracts."""

import json
from pathlib import Path

import pytest

from scholight.survey.finalizer import SurveyFinalizationError, finalize_survey


def _write_run(root: Path) -> None:
    (root / "sections").mkdir()
    (root / "cards").mkdir()
    (root / "00_outline.md").write_text(
        "# Survey Outline\n\n# Title\n\n**A Reliable Survey**\n\n"
        "# Abstract\n\nThis is the first sentence. This is the second sentence.\n\n"
        "# Through-line\n\nEvidence before conclusions.\n",
        encoding="utf-8",
    )
    (root / "sections" / "01_introduction.md").write_text(
        "## Introduction\n\nGrounded evidence [2501.12345].\n",
        encoding="utf-8",
    )
    (root / "sections" / "02_conclusion.md").write_text(
        "## Conclusion\n\nThe conclusion reuses [2501.12345].\n",
        encoding="utf-8",
    )
    (root / "cards" / "2501.12345.md").write_text(
        "# PaperCard\n\n## header\n"
        "- arxiv_id: 2501.12345\n"
        "- title: Verified Evaluation\n"
        "- authors: Example et al.\n"
        "- year/venue: 2025 (arXiv)\n\n"
        "## evidence\nfull_text\n",
        encoding="utf-8",
    )
    (root / "05_research_map.md").write_text("map\n", encoding="utf-8")
    (root / "06_judge_panel.md").write_text("panel\n", encoding="utf-8")


def test_finalizer_preserves_sections_and_deduplicates_references(tmp_path: Path) -> None:
    _write_run(tmp_path)

    result = finalize_survey(tmp_path)

    report = result.report_path.read_text(encoding="utf-8")
    assert report.startswith("# A Reliable Survey\n\n## Abstract\n\nThis is the first sentence")
    assert "## Introduction\n\nGrounded evidence [2501.12345]." in report
    assert "## Conclusion\n\nThe conclusion reuses [2501.12345]." in report
    assert report.count("- [2501.12345]") == 1
    assert "Verified Evaluation" in report
    assert result.section_count == 2
    assert result.reference_count == 1
    assert result.unverified_reference_count == 0
    assert result.evidence_coverage_percent == 100.0
    assert report.count("## Evidence coverage") == 1
    assert "Full or partial text was reviewed for 1 of 1 cited sources." in report
    assert result.index_path.read_text(encoding="utf-8").count("08_survey.md") == 1


@pytest.mark.parametrize("location", ["abstract", "section"])
def test_finalizer_rejects_internal_runtime_details_from_reader_text(
    tmp_path: Path,
    location: str,
) -> None:
    _write_run(tmp_path)
    if location == "abstract":
        (tmp_path / "00_outline.md").write_text(
            "# Survey Outline\n\n# Title\n\nA Reliable Survey\n\n"
            "# Abstract\n\nThe environment is missing pdftotext.\n",
            encoding="utf-8",
        )
    else:
        (tmp_path / "sections" / "01_introduction.md").write_text(
            "## Introduction\n\nRun metadata says pdftotext was unavailable [2501.12345].\n",
            encoding="utf-8",
        )

    with pytest.raises(SurveyFinalizationError) as captured:
        finalize_survey(tmp_path)

    assert captured.value.code == "survey_report_internal_metadata_leaked"


def test_finalizer_accepts_title_and_abstract_below_document_heading(
    tmp_path: Path,
) -> None:
    """RCM writes one document heading followed by level-two outline fields."""
    _write_run(tmp_path)
    (tmp_path / "00_outline.md").write_text(
        "# SurveyOutline — Evaluation Methods\n\n"
        "- run_dir: .\n\n"
        "## Title\n\nA Production Survey\n\n"
        "## Abstract\n\nA production-shaped abstract.\n\n"
        "## Through-line\n\nEvidence before conclusions.\n",
        encoding="utf-8",
    )

    result = finalize_survey(tmp_path)

    report = result.report_path.read_text(encoding="utf-8")
    assert report.startswith(
        "# A Production Survey\n\n## Abstract\n\nA production-shaped abstract."
    )
    assert "run_dir" not in report


@pytest.mark.parametrize(
    ("title_heading", "abstract_heading"),
    [
        ("标题", "摘要"),
        ("标题\uff08Title\uff09", "摘要\uff08abstract\uff0c4\u20136 句\uff09"),
        ("TITLE", "ABSTRACT (4-6 sentences)"),
    ],
)
def test_finalizer_accepts_localized_outline_metadata_headings(
    tmp_path: Path,
    title_heading: str,
    abstract_heading: str,
) -> None:
    _write_run(tmp_path)
    (tmp_path / "00_outline.md").write_text(
        "# SurveyOutline\n\n"
        f"## {title_heading}\n\n可靠的中文综述\n\n"
        f"## {abstract_heading}\n\n这是一个完整摘要。\n\n"
        "## 主线\n\n证据优先。\n",
        encoding="utf-8",
    )

    result = finalize_survey(tmp_path)

    assert result.report_path.read_text(encoding="utf-8").startswith(
        "# 可靠的中文综述\n\n## Abstract\n\n这是一个完整摘要。"
    )


def test_finalizer_exposes_stable_outline_error_code(tmp_path: Path) -> None:
    _write_run(tmp_path)
    (tmp_path / "00_outline.md").write_text(
        "# SurveyOutline\n\n## Through-line\n\nEvidence first.\n",
        encoding="utf-8",
    )

    with pytest.raises(SurveyFinalizationError) as captured:
        finalize_survey(tmp_path)

    assert captured.value.code == "survey_outline_metadata_invalid"


def test_finalizer_prefers_structured_outline_metadata(tmp_path: Path) -> None:
    _write_run(tmp_path)
    (tmp_path / "00_outline.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "title": "The Structured Title",
                "abstract": "The structured abstract wins.",
                "through_line": "Evidence before conclusions.",
            }
        ),
        encoding="utf-8",
    )

    result = finalize_survey(tmp_path)

    assert result.report_path.read_text(encoding="utf-8").startswith(
        "# The Structured Title\n\n## Abstract\n\nThe structured abstract wins."
    )


@pytest.mark.parametrize(
    "outline_json",
    [
        "not json",
        json.dumps({"schema_version": 1, "title": "Missing fields"}),
        json.dumps(
            {
                "schema_version": 2,
                "title": "Wrong version",
                "abstract": "Ignored",
                "through_line": "Ignored",
            }
        ),
    ],
)
def test_finalizer_falls_back_to_markdown_when_outline_json_is_invalid(
    tmp_path: Path,
    outline_json: str,
) -> None:
    _write_run(tmp_path)
    (tmp_path / "00_outline.json").write_text(outline_json, encoding="utf-8")

    result = finalize_survey(tmp_path)

    assert result.report_path.read_text(encoding="utf-8").startswith(
        "# A Reliable Survey\n\n## Abstract\n\nThis is the first sentence"
    )


def test_finalizer_expands_grouped_citations_without_treating_labels_as_ids(
    tmp_path: Path,
) -> None:
    _write_run(tmp_path)
    (tmp_path / "sections" / "01_introduction.md").write_text(
        "## Introduction\n\nCompared evidence [2501.12345, 2502.54321] [Figure 1].\n",
        encoding="utf-8",
    )
    (tmp_path / "cards" / "2502.54321.md").write_text(
        "# PaperCard\n\n## header\n\n- arxiv_id: 2502.54321\n- title: A Second Evaluation\n",
        encoding="utf-8",
    )

    result = finalize_survey(tmp_path)

    report = result.report_path.read_text(encoding="utf-8")
    assert result.reference_count == 2
    assert "[2501.12345]" in report
    assert "[2502.54321]" in report
    assert "cards/Figure 1.md" not in report


def test_finalizer_resolves_legacy_arxiv_citations_to_safe_card_names(tmp_path: Path) -> None:
    _write_run(tmp_path)
    (tmp_path / "sections" / "01_introduction.md").write_text(
        "## Introduction\n\nFoundational evidence [cs/0012009].\n",
        encoding="utf-8",
    )
    (tmp_path / "cards" / "cs-0012009.md").write_text(
        "# PaperCard\n\n- arxiv_id: cs/0012009\n- title: Legacy Evidence\n",
        encoding="utf-8",
    )

    result = finalize_survey(tmp_path)

    report = result.report_path.read_text(encoding="utf-8")
    assert "- [cs/0012009] **Legacy Evidence.** arXiv:cs/0012009." in report
    assert result.reference_count == 2
    assert result.unverified_reference_count == 0


def test_finalizer_includes_only_an_existing_safe_figure(tmp_path: Path) -> None:
    _write_run(tmp_path)
    (tmp_path / "08_global_picture.png").write_bytes(b"png")

    finalize_survey(tmp_path)

    report = (tmp_path / "08_survey.md").read_text(encoding="utf-8")
    assert "![Global picture of the field](08_global_picture.png)" in report


def test_finalizer_accepts_bold_metadata_without_header_section(tmp_path: Path) -> None:
    _write_run(tmp_path)
    (tmp_path / "cards" / "2501.12345.md").write_text(
        "# PaperCard — Verified Evaluation\n\n"
        "- **arXiv id**: 2501.12345\n"
        "- **title**: Verified Evaluation\n"
        "- **authors**: Example et al.\n"
        "- **year/venue**: 2025 arXiv\n\n"
        "## problem\n\nA tested problem.\n",
        encoding="utf-8",
    )

    result = finalize_survey(tmp_path)

    assert result.reference_count == 1
    assert "Verified Evaluation" in result.report_path.read_text(encoding="utf-8")


def test_finalizer_marks_missing_cited_card_as_unverified(tmp_path: Path) -> None:
    _write_run(tmp_path)
    (tmp_path / "sections" / "02_conclusion.md").write_text(
        "## Conclusion\n\nVerified [2501.12345] and unavailable [2606.19544].\n",
        encoding="utf-8",
    )

    result = finalize_survey(tmp_path)

    report = result.report_path.read_text(encoding="utf-8")
    assert result.reference_count == 2
    assert result.unverified_reference_count == 1
    assert "Bibliographic details could not be verified" in report
    assert "PaperCard" not in report
    assert "arXiv:2606.19544" in report


def test_finalizer_marks_incomplete_cited_card_as_unverified(tmp_path: Path) -> None:
    _write_run(tmp_path)
    (tmp_path / "sections" / "02_conclusion.md").write_text(
        "## Conclusion\n\nVerified [2501.12345] and incomplete [1904.06505].\n",
        encoding="utf-8",
    )
    (tmp_path / "cards" / "1904.06505.md").write_text(
        "# PaperCard\n\n- arxiv_id: 1904.06505\n\n## problem\n\nEvidence without metadata.\n",
        encoding="utf-8",
    )

    result = finalize_survey(tmp_path)

    report = result.report_path.read_text(encoding="utf-8")
    assert result.reference_count == 2
    assert result.unverified_reference_count == 1
    assert "Bibliographic details could not be verified" in report
    assert "PaperCard" not in report
    assert "arXiv:1904.06505" in report


def test_finalizer_rejects_run_without_any_verified_paper_card(tmp_path: Path) -> None:
    _write_run(tmp_path)
    (tmp_path / "cards" / "2501.12345.md").unlink()

    with pytest.raises(SurveyFinalizationError, match="no verified paper cards"):
        finalize_survey(tmp_path)

    assert not (tmp_path / "08_survey.md").exists()
    assert not (tmp_path / "index.md").exists()


def test_finalizer_rejects_run_when_every_paper_card_is_incomplete(tmp_path: Path) -> None:
    _write_run(tmp_path)
    (tmp_path / "cards" / "2501.12345.md").write_text(
        "# PaperCard\n\n- arxiv_id: 2501.12345\n",
        encoding="utf-8",
    )

    with pytest.raises(SurveyFinalizationError, match="no verified paper cards"):
        finalize_survey(tmp_path)

    assert not (tmp_path / "08_survey.md").exists()
    assert not (tmp_path / "index.md").exists()


def test_finalizer_rejects_section_symlink(tmp_path: Path) -> None:
    _write_run(tmp_path)
    target = tmp_path / "outside.md"
    target.write_text("## Replaced\n\n[2501.12345]\n", encoding="utf-8")
    section = tmp_path / "sections" / "01_introduction.md"
    section.unlink()
    section.symlink_to(target)

    with pytest.raises(SurveyFinalizationError, match="Invalid section artifact"):
        finalize_survey(tmp_path)


def test_finalizer_rejects_symlinked_card_directory(tmp_path: Path) -> None:
    _write_run(tmp_path)
    external_cards = tmp_path / "external-cards"
    (tmp_path / "cards").rename(external_cards)
    (tmp_path / "cards").symlink_to(external_cards, target_is_directory=True)

    with pytest.raises(SurveyFinalizationError, match="Required directory is invalid: cards"):
        finalize_survey(tmp_path)


_VALID_CHART = """## Comparison

Compared methods [2501.12345] with shared metrics.

```chart
{
  "type": "line",
  "title": "Accuracy vs context length",
  "x": [1024, 2048],
  "series": [{"name": "Method A", "y": [72.1, 71.8]}],
  "caption": "Shared benchmark; values from cited cards."
}
```

The loss is $\\mathcal{L}(\\theta)$ inline.

$$
E = mc^2
$$

| Method | Score |
| --- | --- |
| A | 72.1 |
"""


def test_finalizer_renders_chart_blocks_and_preserves_formulas_and_tables(
    tmp_path: Path,
) -> None:
    _write_run(tmp_path)
    (tmp_path / "sections" / "01_introduction.md").write_text(_VALID_CHART, encoding="utf-8")

    result = finalize_survey(tmp_path)

    report = result.report_path.read_text(encoding="utf-8")
    assert "![Shared benchmark; values from cited cards.](figures/01_introduction-1.png)" in report
    assert "```chart" not in report
    assert (tmp_path / "figures" / "01_introduction-1.png").read_bytes().startswith(b"\x89PNG")
    assert "$\\mathcal{L}(\\theta)$" in report
    assert "$$\nE = mc^2\n$$" in report
    assert "| Method | Score |" in report
    assert result.chart_count == 1
    assert result.chart_rejected_count == 0


def test_finalizer_drops_invalid_chart_blocks_and_counts_them(tmp_path: Path) -> None:
    _write_run(tmp_path)
    (tmp_path / "sections" / "01_introduction.md").write_text(
        "## Introduction\n\n"
        "Evidence [2501.12345] survives.\n\n"
        "```chart\n"
        '{"type": "line", "series": []}\n'
        "```\n",
        encoding="utf-8",
    )

    result = finalize_survey(tmp_path)

    report = result.report_path.read_text(encoding="utf-8")
    assert "```chart" not in report
    assert "Evidence [2501.12345] survives." in report
    assert result.chart_count == 0
    assert result.chart_rejected_count == 1
    assert not (tmp_path / "figures").exists() or not list((tmp_path / "figures").iterdir())


def test_finalizer_without_charts_keeps_zero_counts(tmp_path: Path) -> None:
    _write_run(tmp_path)

    result = finalize_survey(tmp_path)

    assert result.chart_count == 0
    assert result.chart_rejected_count == 0


def test_finalizer_caps_document_wide_chart_blocks_at_eight(tmp_path: Path) -> None:
    _write_run(tmp_path)
    chart = json.dumps(
        {
            "type": "line",
            "title": "Chart",
            "x": [1, 2],
            "series": [{"name": "s", "y": [1.0, 2.0]}],
        }
    )

    def _blocks(count: int) -> str:
        return "\n\n".join(f"```chart\n{chart}\n```" for _ in range(count))

    (tmp_path / "sections" / "01_introduction.md").write_text(
        f"## Introduction\n\nEvidence [2501.12345].\n\n{_blocks(5)}\n",
        encoding="utf-8",
    )
    (tmp_path / "sections" / "02_conclusion.md").write_text(
        f"## Conclusion\n\nMore [2501.12345].\n\n{_blocks(5)}\n",
        encoding="utf-8",
    )

    result = finalize_survey(tmp_path)

    report = result.report_path.read_text(encoding="utf-8")
    assert result.chart_count == 8
    assert result.chart_rejected_count == 2
    assert "```chart" not in report
    assert report.count("![Chart](figures/") == 8
