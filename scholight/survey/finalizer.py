"""Deterministic assembly of a completed Survey from validated stage artifacts."""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

import structlog

from scholight.sources.arxiv import arxiv_artifact_stem
from scholight.survey.evidence import paper_evidence_level

logger = structlog.get_logger(__name__)

_BRACKETED_CITATIONS = re.compile(r"\[([^\]\n]{1,1024})\]")
_ARXIV_ID = re.compile(
    r"(?<![A-Za-z0-9.])(?:\d{4}\.\d{4,5}|[a-z][a-z-]+/\d{7})(?:v\d+)?"
    r"(?![A-Za-z0-9.])"
)
_SECTION_FILE = re.compile(r"^\d{2}_[A-Za-z0-9-]+\.md$")
_OUTLINE_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_CARD_FIELD = re.compile(r"^-\s+(.+?):\s*(.*?)\s*$")
_OUTLINE_JSON_MAX_BYTES = 1024 * 1024
_CHART_MAX_PER_DOCUMENT = 8
_INTERNAL_REPORT_MARKER = re.compile(
    r"(?:\brun metadata\b|\bPaperCard\b|\bpdftotext\b|"
    r"\b(?:this|current) environment\b.{0,60}(?:unavailable|missing|not installed)|"
    r"(?:本环境|运行环境).{0,40}(?:缺少|没有|不可用))",
    re.I,
)


class SurveyFinalizationError(RuntimeError):
    """A stable, client-safe deterministic finalization failure."""

    def __init__(self, message: str, *, code: str = "survey_artifact_contract_invalid") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FinalizedSurvey:
    """Metadata for the two application-owned final artifacts."""

    report_path: Path
    index_path: Path
    section_count: int
    reference_count: int
    unverified_reference_count: int
    evidence_coverage_percent: float
    chart_count: int = 0
    chart_rejected_count: int = 0


def _regular_file(
    root: Path,
    relative_path: str,
    *,
    required: bool = True,
    error_code: str = "survey_artifact_contract_invalid",
) -> Path | None:
    candidate = root / relative_path
    try:
        candidate_stat = candidate.lstat()
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except OSError as exc:
        if required:
            raise SurveyFinalizationError(
                f"Required artifact is unavailable: {relative_path}",
                code=error_code,
            ) from exc
        return None
    if (
        not stat.S_ISREG(candidate_stat.st_mode)
        or candidate.is_symlink()
        or resolved_candidate.parent != resolved_root
        or candidate_stat.st_size <= 0
    ):
        if required:
            raise SurveyFinalizationError(
                f"Required artifact is invalid: {relative_path}",
                code=error_code,
            )
        return None
    return candidate


def _read_text(path: Path, *, label: str) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SurveyFinalizationError(f"Unable to read {label}") from exc
    if not content.strip():
        raise SurveyFinalizationError(f"Required artifact is empty: {label}")
    return content


def _safe_directory(run_root: Path, relative_path: str) -> tuple[Path, Path]:
    directory = run_root / relative_path
    try:
        directory_stat = directory.lstat()
        resolved_run_root = run_root.resolve(strict=True)
        resolved_directory = directory.resolve(strict=True)
    except OSError as exc:
        raise SurveyFinalizationError(
            f"Required directory is unavailable: {relative_path}"
        ) from exc
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or directory.is_symlink()
        or resolved_directory.parent != resolved_run_root
    ):
        raise SurveyFinalizationError(f"Required directory is invalid: {relative_path}")
    return directory, resolved_directory


def _outline_fields(outline: str) -> tuple[str, str]:
    sections: dict[str, list[str]] = {}
    active: str | None = None
    for line in outline.splitlines():
        heading = _OUTLINE_HEADING.match(line.strip())
        if heading is not None:
            active = _outline_heading_key(heading.group(1))
            sections.setdefault(active, [])
            continue
        if active is not None:
            sections[active].append(line)

    title_lines = sections.get("title", [])
    title = " ".join(line.strip() for line in title_lines if line.strip()).strip()
    title = re.sub(r"^\*\*(.*?)\*\*$", r"\1", title).strip()
    abstract = "\n".join(sections.get("abstract", [])).strip()
    if not title or not abstract:
        raise SurveyFinalizationError(
            "Outline must contain non-empty Title and Abstract sections",
            code="survey_outline_metadata_invalid",
        )
    return title, abstract


def _structured_outline_fields(run_root: Path) -> tuple[str, str] | None:
    outline_path = _regular_file(run_root, "00_outline.json", required=False)
    if outline_path is None:
        logger.warning("survey_outline_json_fallback", warning_code="outline_json_missing")
        return None
    try:
        if outline_path.stat().st_size > _OUTLINE_JSON_MAX_BYTES:
            raise ValueError("outline JSON exceeds limit")
        payload = json.loads(outline_path.read_bytes())
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        logger.warning("survey_outline_json_fallback", warning_code="outline_json_invalid")
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        logger.warning("survey_outline_json_fallback", warning_code="outline_json_invalid")
        return None
    title = payload.get("title")
    abstract = payload.get("abstract")
    through_line = payload.get("through_line")
    if (
        not isinstance(title, str)
        or not title.strip()
        or not isinstance(abstract, str)
        or not abstract.strip()
        or not isinstance(through_line, str)
        or not through_line.strip()
    ):
        logger.warning("survey_outline_json_fallback", warning_code="outline_json_invalid")
        return None
    normalized_title = " ".join(title.split())
    normalized_abstract = abstract.strip()
    if len(normalized_title) > 500 or len(normalized_abstract) > 20_000:
        logger.warning("survey_outline_json_fallback", warning_code="outline_json_invalid")
        return None
    return normalized_title, normalized_abstract


def _outline_heading_key(value: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).strip()
    normalized = re.sub(r"\s+#+\s*$", "", normalized).strip(" *_:-")
    base = re.split(r"\s*\(", normalized, maxsplit=1)[0].strip().casefold()
    aliases = {
        "title": "title",
        "标题": "title",
        "abstract": "abstract",
        "摘要": "abstract",
    }
    return aliases.get(base, base)


def _section_files(run_root: Path) -> list[Path]:
    section_root, resolved_section_root = _safe_directory(run_root, "sections")

    paths: list[Path] = []
    try:
        candidates = sorted(section_root.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise SurveyFinalizationError("Unable to list section artifacts") from exc
    for candidate in candidates:
        if not _SECTION_FILE.fullmatch(candidate.name):
            continue
        try:
            candidate_stat = candidate.lstat()
            resolved_candidate = candidate.resolve(strict=True)
        except OSError as exc:
            raise SurveyFinalizationError(f"Unable to inspect section: {candidate.name}") from exc
        if (
            not stat.S_ISREG(candidate_stat.st_mode)
            or candidate.is_symlink()
            or resolved_candidate.parent != resolved_section_root
            or candidate_stat.st_size <= 0
        ):
            raise SurveyFinalizationError(f"Invalid section artifact: {candidate.name}")
        paths.append(candidate)
    if not paths:
        raise SurveyFinalizationError(
            "No completed section artifacts were found",
            code="survey_section_contract_invalid",
        )
    return paths


def _card_metadata(card: str) -> tuple[str | None, str | None, str | None]:
    fields: dict[str, str] = {}
    for line in card.splitlines():
        stripped = line.strip()
        if stripped.casefold() == "## problem":
            break
        match = _CARD_FIELD.match(stripped)
        if match is not None:
            key = match.group(1).strip().strip("*").casefold().replace("_", " ")
            fields[key] = match.group(2).strip()

    title = fields.get("title")
    authors = fields.get("authors")
    year_venue = fields.get("year/venue")
    return title, authors, year_venue


def _unverified_reference(citation_id: str) -> str:
    return (
        f"- [{citation_id}] arXiv:{citation_id}. "
        "Bibliographic details could not be verified; consult the arXiv record."
    )


def _references(run_root: Path, section_texts: list[str]) -> tuple[str, int, int, int]:
    citation_ids: list[str] = []
    for section in section_texts:
        for bracketed in _BRACKETED_CITATIONS.findall(section):
            for citation_id in _ARXIV_ID.findall(bracketed):
                if citation_id not in citation_ids:
                    citation_ids.append(citation_id)
    if not citation_ids:
        raise SurveyFinalizationError(
            "Completed sections contain no auditable citations",
            code="survey_reference_contract_invalid",
        )

    entries: list[str] = []
    verified_count = 0
    unverified_count = 0
    reviewed_count = 0
    card_root, resolved_card_root = _safe_directory(run_root, "cards")
    for citation_id in citation_ids:
        card_stem = arxiv_artifact_stem(citation_id)
        if card_stem is None:
            entries.append(_unverified_reference(citation_id))
            unverified_count += 1
            continue
        card_path = card_root / f"{card_stem}.md"
        try:
            card_stat = card_path.lstat()
            resolved_card = card_path.resolve(strict=True)
        except FileNotFoundError:
            entries.append(_unverified_reference(citation_id))
            unverified_count += 1
            continue
        except OSError as exc:
            raise SurveyFinalizationError(
                f"Unable to inspect cited paper card: {citation_id}"
            ) from exc
        if (
            not stat.S_ISREG(card_stat.st_mode)
            or card_path.is_symlink()
            or resolved_card.parent != resolved_card_root
            or card_stat.st_size <= 0
        ):
            raise SurveyFinalizationError(f"Cited paper card is invalid: {citation_id}")
        card = _read_text(card_path, label=f"cards/{card_stem}.md")
        if paper_evidence_level(card) in {"html", "full_text", "partial"}:
            reviewed_count += 1
        title, authors, year_venue = _card_metadata(card)
        if not title:
            entries.append(_unverified_reference(citation_id))
            unverified_count += 1
            continue
        details = [value.rstrip(".") for value in (authors, year_venue) if value]
        suffix = f" {'; '.join(details)}." if details else ""
        entries.append(f"- [{citation_id}] **{title}.**{suffix} arXiv:{citation_id}.")
        verified_count += 1
    if verified_count == 0:
        raise SurveyFinalizationError(
            "Completed sections contain no verified paper cards",
            code="survey_reference_contract_invalid",
        )
    return "\n".join(entries), len(entries), unverified_count, reviewed_count


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
        raise SurveyFinalizationError(
            f"Unable to write final artifact: {path.name}",
            code="survey_finalization_write_failed",
        ) from exc


def finalize_survey(run_root: Path) -> FinalizedSurvey:
    """Build the final report and manifest without another model completion."""
    outline_path = _regular_file(
        run_root,
        "00_outline.md",
        error_code="survey_outline_metadata_invalid",
    )
    if outline_path is None:
        raise SurveyFinalizationError(
            "Required artifact is missing: 00_outline.md",
            code="survey_outline_metadata_invalid",
        )
    title, abstract = _structured_outline_fields(run_root) or _outline_fields(
        _read_text(outline_path, label="00_outline.md")
    )

    sections = _section_files(run_root)
    section_texts = [_read_text(path, label=f"sections/{path.name}").strip() for path in sections]
    from scholight.survey.charts import render_section_charts
    from scholight.survey.math_format import normalize_report_math

    chart_count = 0
    chart_rejected_count = 0
    rendered_sections: list[str] = []
    for section_path, section_text in zip(sections, section_texts, strict=True):
        rendered_text, rendered_n, rejected_n = render_section_charts(
            section_text,
            run_root / "figures",
            prefix=section_path.stem,
            render_budget=_CHART_MAX_PER_DOCUMENT - chart_count,
        )
        rendered_sections.append(rendered_text)
        chart_count += rendered_n
        chart_rejected_count += rejected_n
    section_texts = rendered_sections
    reader_facing_text = [abstract, *section_texts]
    if any(_INTERNAL_REPORT_MARKER.search(text) is not None for text in reader_facing_text):
        raise SurveyFinalizationError(
            "A report section contains internal runtime metadata",
            code="survey_report_internal_metadata_leaked",
        )
    references, reference_count, unverified_reference_count, reviewed_reference_count = _references(
        run_root, section_texts
    )
    evidence_coverage_percent = round(100.0 * reviewed_reference_count / reference_count, 2)

    report_parts = [f"# {title}", "## Abstract", abstract]
    if _regular_file(run_root, "08_global_picture.png", required=False) is not None:
        report_parts.extend(
            [
                "![Global picture of the field](08_global_picture.png)",
                "The figure provides a visual orientation to the landscape discussed below.",
            ]
        )
    report_parts.extend(section_texts)
    report_parts.extend(
        [
            "## Evidence coverage",
            (
                "Full or partial text was reviewed for "
                f"{reviewed_reference_count} of {reference_count} cited sources. "
                f"{reference_count - reviewed_reference_count} sources rely on abstract or "
                "bibliographic evidence only."
            ),
            "## References",
            references,
        ]
    )
    report = normalize_report_math("\n\n".join(report_parts).rstrip() + "\n")

    index_items = [
        "- [Final survey](08_survey.md)",
        "- [Survey outline](00_outline.md)",
        "- [Research map](05_research_map.md)",
        "- [Judge panel](06_judge_panel.md)",
        "- [Expanded sections](sections/)",
        "- [Paper cards](cards/)",
    ]
    if _regular_file(run_root, "00_outline.json", required=False) is not None:
        index_items.insert(2, "- [Structured survey outline](00_outline.json)")
    if _regular_file(run_root, "08_global_picture.png", required=False) is not None:
        index_items.append("- [Global picture](08_global_picture.png)")
    index = "# Survey artifacts\n\n" + "\n".join(index_items) + "\n"

    report_path = run_root / "08_survey.md"
    index_path = run_root / "index.md"
    _atomic_write(report_path, report)
    _atomic_write(index_path, index)
    return FinalizedSurvey(
        report_path=report_path,
        index_path=index_path,
        section_count=len(section_texts),
        reference_count=reference_count,
        unverified_reference_count=unverified_reference_count,
        evidence_coverage_percent=evidence_coverage_percent,
        chart_count=chart_count,
        chart_rejected_count=chart_rejected_count,
    )


__all__ = ["FinalizedSurvey", "SurveyFinalizationError", "finalize_survey"]
