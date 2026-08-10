"""Deterministic Survey final assembly contracts."""

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
    assert result.index_path.read_text(encoding="utf-8").count("08_survey.md") == 1


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
    assert "PaperCard metadata was unavailable" in report
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
    assert "PaperCard metadata was unavailable or incomplete" in report
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
