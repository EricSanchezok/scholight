"""Bounded full-text evidence inspection for completed Survey workspaces."""

from __future__ import annotations

import re
import stat
from dataclasses import dataclass
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

_CARD_MAX_BYTES = 1024 * 1024
_LEVELS = ("html", "full_text", "partial", "abstract_only")
_REVIEWED_LEVELS = frozenset({"html", "full_text", "partial"})
_LEVEL_REASONS = {
    "html": frozenset({"html_text_extracted"}),
    "full_text": frozenset({"pdf_text_extracted"}),
    "partial": frozenset({"pdf_text_truncated"}),
    "abstract_only": frozenset(
        {"scanned_pdf", "pdf_download_failed", "pdf_text_empty", "pdf_extraction_failed"}
    ),
}
_EVIDENCE_HEADING = re.compile(r"(?im)^#{1,6}\s+evidence\s*$")
_LEVEL_FIELD = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:level|evidence)\s*:\s*"
    r"(html|full_text|partial|abstract_only)\s*$"
)
_REASON_FIELD = re.compile(r"(?im)^\s*(?:[-*]\s*)?reason\s*:\s*([a-z0-9_]+)\s*$")
_LEGACY_LEVEL = re.compile(r"(?im)^\s*(html|full_text|partial|abstract_only)\s*$")
_RUNTIME_MARKERS = (
    re.compile(r"pdftotext.{0,80}(?:unavailable|not installed|missing|not found)", re.I | re.S),
    re.compile(r"(?:unavailable|not installed|missing|not found).{0,80}pdftotext", re.I | re.S),
    re.compile(r"(?:本环境|运行环境).{0,40}(?:缺少|没有|不可用).{0,40}pdftotext", re.S),
)


class SurveyEvidenceAuditError(RuntimeError):
    """A stable, client-safe failure in the full-text evidence contract."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class SurveyEvidenceSummary:
    """Aggregate evidence levels without retaining paper content."""

    card_count: int
    counts: dict[str, int]
    reviewed_count: int
    coverage_percent: float
    invalid_reason_count: int
    runtime_marker_count: int


def _card_documents(run_root: Path) -> list[str]:
    cards = run_root / "cards"
    try:
        cards_stat = cards.lstat()
        resolved_cards = cards.resolve(strict=True)
        resolved_root = run_root.resolve(strict=True)
    except OSError:
        return []
    if (
        not stat.S_ISDIR(cards_stat.st_mode)
        or cards.is_symlink()
        or resolved_cards.parent != resolved_root
    ):
        return []

    documents: list[str] = []
    for path in sorted(cards.glob("*.md"), key=lambda candidate: candidate.name):
        try:
            path_stat = path.lstat()
            resolved_path = path.resolve(strict=True)
        except OSError:
            continue
        if (
            not stat.S_ISREG(path_stat.st_mode)
            or path.is_symlink()
            or resolved_path.parent != resolved_cards
            or path_stat.st_size <= 0
            or path_stat.st_size > _CARD_MAX_BYTES
        ):
            continue
        try:
            documents.append(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
    return documents


def _evidence_block(document: str) -> str:
    heading = _EVIDENCE_HEADING.search(document)
    if heading is None:
        return document
    block = document[heading.end() :]
    next_heading = re.search(r"(?m)^#{1,6}\s+", block)
    return block[: next_heading.start()] if next_heading is not None else block


def _evidence_fields(document: str) -> tuple[str, str | None]:
    block = _evidence_block(document)
    level_match = _LEVEL_FIELD.search(block) or _LEGACY_LEVEL.search(block)
    level = level_match.group(1).casefold() if level_match is not None else "unknown"
    reason_match = _REASON_FIELD.search(block)
    reason = reason_match.group(1).casefold() if reason_match is not None else None
    return level, reason


def paper_evidence_level(document: str) -> str:
    """Return one stable evidence level from a PaperCard document."""
    return _evidence_fields(document)[0]


def summarize_survey_evidence(run_root: Path) -> SurveyEvidenceSummary:
    """Summarize card evidence using bounded local reads only."""
    documents = _card_documents(run_root)
    counts = dict.fromkeys((*_LEVELS, "unknown"), 0)
    invalid_reason_count = 0
    runtime_marker_count = 0
    for document in documents:
        level, reason = _evidence_fields(document)
        counts[level if level in counts else "unknown"] += 1
        if reason not in _LEVEL_REASONS.get(level, frozenset()):
            invalid_reason_count += 1
        if any(marker.search(document) is not None for marker in _RUNTIME_MARKERS):
            runtime_marker_count += 1
    reviewed_count = sum(counts[level] for level in _REVIEWED_LEVELS)
    coverage = 100.0 * reviewed_count / len(documents) if documents else 0.0
    return SurveyEvidenceSummary(
        card_count=len(documents),
        counts=counts,
        reviewed_count=reviewed_count,
        coverage_percent=round(coverage, 2),
        invalid_reason_count=invalid_reason_count,
        runtime_marker_count=runtime_marker_count,
    )


def audit_survey_evidence(run_root: Path) -> SurveyEvidenceSummary:
    """Enforce full-worker evidence requirements before deterministic finalization."""
    summary = summarize_survey_evidence(run_root)
    if summary.runtime_marker_count:
        raise SurveyEvidenceAuditError(
            "The Survey full-text runtime was unavailable.",
            code="survey_full_text_runtime_unavailable",
        )
    if summary.card_count and summary.reviewed_count == 0:
        raise SurveyEvidenceAuditError(
            "The Survey did not produce evidence from paper bodies.",
            code="survey_full_text_evidence_missing",
        )
    if summary.card_count and (summary.counts["unknown"] > 0 or summary.invalid_reason_count > 0):
        raise SurveyEvidenceAuditError(
            "The Survey paper evidence declarations are incomplete.",
            code="survey_full_text_evidence_invalid",
        )
    if summary.card_count >= 10 and summary.counts["abstract_only"] * 2 > summary.card_count:
        logger.warning(
            "survey_full_text_coverage_low",
            card_count=summary.card_count,
            coverage_percent=summary.coverage_percent,
        )
    return summary


__all__ = [
    "SurveyEvidenceAuditError",
    "SurveyEvidenceSummary",
    "audit_survey_evidence",
    "paper_evidence_level",
    "summarize_survey_evidence",
]
