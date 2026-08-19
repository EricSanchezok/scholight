"""Survey full-text evidence audit contracts."""

from pathlib import Path

import pytest

from scholight.survey.evidence import (
    SurveyEvidenceAuditError,
    audit_survey_evidence,
    summarize_survey_evidence,
)


def _card(root: Path, name: str, evidence: str) -> None:
    cards = root / "cards"
    cards.mkdir(exist_ok=True)
    (cards / name).write_text(
        f"# PaperCard\n\n## header\n- title: Evidence\n\n## evidence\n{evidence}\n",
        encoding="utf-8",
    )


def test_evidence_summary_counts_new_and_legacy_card_shapes(tmp_path: Path) -> None:
    _card(tmp_path, "full.md", "- level: full_text\n- reason: pdf_text_extracted")
    _card(tmp_path, "partial.md", "partial")
    _card(tmp_path, "abstract.md", "- level: abstract_only\n- reason: scanned_pdf")
    _card(tmp_path, "html.md", "html")

    summary = summarize_survey_evidence(tmp_path)

    assert summary.card_count == 4
    assert summary.counts == {
        "html": 1,
        "full_text": 1,
        "partial": 1,
        "abstract_only": 1,
        "unknown": 0,
    }
    assert summary.reviewed_count == 3
    assert summary.coverage_percent == 75.0


@pytest.mark.parametrize(
    "message",
    [
        "pdftotext unavailable in this environment",
        "pdftotext is not installed",
        "本环境缺少 pdftotext",
    ],
)
def test_runtime_dependency_leaks_fail_the_evidence_audit(
    tmp_path: Path,
    message: str,
) -> None:
    _card(tmp_path, "paper.md", f"- level: abstract_only\n- reason: {message}")

    with pytest.raises(SurveyEvidenceAuditError) as captured:
        audit_survey_evidence(tmp_path)

    assert captured.value.code == "survey_full_text_runtime_unavailable"


def test_cards_without_any_body_evidence_fail_the_evidence_audit(tmp_path: Path) -> None:
    for index in range(3):
        _card(
            tmp_path,
            f"paper-{index}.md",
            "- level: abstract_only\n- reason: pdf_download_failed",
        )

    with pytest.raises(SurveyEvidenceAuditError) as captured:
        audit_survey_evidence(tmp_path)

    assert captured.value.code == "survey_full_text_evidence_missing"


def test_real_partial_evidence_allows_completion(tmp_path: Path) -> None:
    _card(tmp_path, "paper.md", "- level: partial\n- reason: pdf_text_truncated")

    summary = audit_survey_evidence(tmp_path)

    assert summary.coverage_percent == 100.0


def test_evidence_reason_must_match_the_declared_level(tmp_path: Path) -> None:
    _card(tmp_path, "paper.md", "- level: full_text\n- reason: scanned_pdf")

    with pytest.raises(SurveyEvidenceAuditError) as captured:
        audit_survey_evidence(tmp_path)

    assert captured.value.code == "survey_full_text_evidence_invalid"


@pytest.mark.parametrize(
    "reason",
    ["scanned_pdf", "pdf_download_failed", "pdf_text_empty", "pdf_extraction_failed"],
)
def test_real_abstract_fallback_reasons_are_accepted_when_body_evidence_exists(
    tmp_path: Path,
    reason: str,
) -> None:
    _card(tmp_path, "reviewed.md", "- level: full_text\n- reason: pdf_text_extracted")
    _card(tmp_path, "fallback.md", f"- level: abstract_only\n- reason: {reason}")

    summary = audit_survey_evidence(tmp_path)

    assert summary.coverage_percent == 50.0
    assert summary.counts["abstract_only"] == 1
