"""Deterministic assembly of a completed Survey from validated stage artifacts."""

from __future__ import annotations

import os
import re
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

_BRACKETED_CITATIONS = re.compile(r"\[([^\]\n]{1,1024})\]")
_ARXIV_ID = re.compile(r"(?<![A-Za-z0-9.])\d{4}\.\d{4,5}(?:v\d+)?(?![A-Za-z0-9.])")
_SECTION_FILE = re.compile(r"^\d{2}_[A-Za-z0-9-]+\.md$")
_OUTLINE_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$")
_CARD_FIELD = re.compile(r"^-\s+(.+?):\s*(.*?)\s*$")


class SurveyFinalizationError(RuntimeError):
    """Raised when deterministic final assembly cannot prove its inputs are complete."""


@dataclass(frozen=True, slots=True)
class FinalizedSurvey:
    """Metadata for the two application-owned final artifacts."""

    report_path: Path
    index_path: Path
    section_count: int
    reference_count: int
    unverified_reference_count: int


def _regular_file(root: Path, relative_path: str, *, required: bool = True) -> Path | None:
    candidate = root / relative_path
    try:
        candidate_stat = candidate.lstat()
        resolved_root = root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except OSError as exc:
        if required:
            raise SurveyFinalizationError(
                f"Required artifact is unavailable: {relative_path}"
            ) from exc
        return None
    if (
        not stat.S_ISREG(candidate_stat.st_mode)
        or candidate.is_symlink()
        or resolved_candidate.parent != resolved_root
        or candidate_stat.st_size <= 0
    ):
        if required:
            raise SurveyFinalizationError(f"Required artifact is invalid: {relative_path}")
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
            active = heading.group(1).strip().casefold()
            sections.setdefault(active, [])
            continue
        if active is not None:
            sections[active].append(line)

    title_lines = sections.get("title", [])
    title = " ".join(line.strip() for line in title_lines if line.strip()).strip()
    title = re.sub(r"^\*\*(.*?)\*\*$", r"\1", title).strip()
    abstract = "\n".join(sections.get("abstract", [])).strip()
    if not title or not abstract:
        raise SurveyFinalizationError("Outline must contain non-empty Title and Abstract sections")
    return title, abstract


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
        raise SurveyFinalizationError("No completed section artifacts were found")
    return paths


def _card_metadata(card: str, *, citation_id: str) -> tuple[str, str | None, str | None]:
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
    if not title:
        raise SurveyFinalizationError(f"Paper card has no title metadata: {citation_id}")
    authors = fields.get("authors")
    year_venue = fields.get("year/venue")
    return title, authors, year_venue


def _references(run_root: Path, section_texts: list[str]) -> tuple[str, int, int]:
    citation_ids: list[str] = []
    for section in section_texts:
        for bracketed in _BRACKETED_CITATIONS.findall(section):
            for citation_id in _ARXIV_ID.findall(bracketed):
                if citation_id not in citation_ids:
                    citation_ids.append(citation_id)
    if not citation_ids:
        raise SurveyFinalizationError("Completed sections contain no auditable citations")

    entries: list[str] = []
    verified_count = 0
    unverified_count = 0
    card_root, resolved_card_root = _safe_directory(run_root, "cards")
    for citation_id in citation_ids:
        card_path = card_root / f"{citation_id}.md"
        try:
            card_stat = card_path.lstat()
            resolved_card = card_path.resolve(strict=True)
        except FileNotFoundError:
            entries.append(
                f"- [{citation_id}] arXiv:{citation_id}. "
                "PaperCard metadata was unavailable in this run; treat this citation as unverified."
            )
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
        title, authors, year_venue = _card_metadata(
            _read_text(card_path, label=f"cards/{citation_id}.md"),
            citation_id=citation_id,
        )
        details = [value.rstrip(".") for value in (authors, year_venue) if value]
        suffix = f" {'; '.join(details)}." if details else ""
        entries.append(f"- [{citation_id}] **{title}.**{suffix} arXiv:{citation_id}.")
        verified_count += 1
    if verified_count == 0:
        raise SurveyFinalizationError("Completed sections contain no verified paper cards")
    return "\n".join(entries), len(entries), unverified_count


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
        raise SurveyFinalizationError(f"Unable to write final artifact: {path.name}") from exc


def finalize_survey(run_root: Path) -> FinalizedSurvey:
    """Build the final report and manifest without another model completion."""
    outline_path = _regular_file(run_root, "00_outline.md")
    assert outline_path is not None
    title, abstract = _outline_fields(_read_text(outline_path, label="00_outline.md"))

    sections = _section_files(run_root)
    section_texts = [_read_text(path, label=f"sections/{path.name}").strip() for path in sections]
    references, reference_count, unverified_reference_count = _references(run_root, section_texts)

    report_parts = [f"# {title}", "## Abstract", abstract]
    if _regular_file(run_root, "08_global_picture.png", required=False) is not None:
        report_parts.extend(
            [
                "![Global picture of the field](08_global_picture.png)",
                "The figure provides a visual orientation to the landscape discussed below.",
            ]
        )
    report_parts.extend(section_texts)
    report_parts.extend(["## References", references])
    report = "\n\n".join(report_parts).rstrip() + "\n"

    index_items = [
        "- [Final survey](08_survey.md)",
        "- [Survey outline](00_outline.md)",
        "- [Research map](05_research_map.md)",
        "- [Judge panel](06_judge_panel.md)",
        "- [Expanded sections](sections/)",
        "- [Paper cards](cards/)",
    ]
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
    )


__all__ = ["FinalizedSurvey", "SurveyFinalizationError", "finalize_survey"]
