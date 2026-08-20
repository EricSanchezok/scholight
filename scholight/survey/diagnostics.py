"""Best-effort, runtime-owned diagnostics for the Survey workflow."""

from __future__ import annotations

import json
import os
import re
import stat
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from scholight.sources.arxiv import arxiv_artifact_stem, canonicalize_arxiv_id

logger = structlog.get_logger(__name__)

TRACE_FILE = "trajectory.jsonl"
DIAGNOSTICS_FILE = "diagnostics.json"
_MAX_STRING_LENGTH = 2_048
_MAX_QUERY_LENGTH = 500
_MAX_COLLECTION_ITEMS = 64
_MAX_EXPECTED_OUTPUTS = 1_024
_MAX_PLAN_BYTES = 1_048_576
_MAX_CARD_PLAN_ITEMS = 100
_MAX_SECTION_PLAN_ITEMS = 128
_SECRET_KEY = re.compile(r"(?i)(authorization|api[_-]?key|token|secret|password|jwt)")
_SECRET_VALUE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~-]+|sk_(?:live|test)_[A-Za-z0-9._~-]+")
_ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9._~-])/(?:[^\s:'\"]+/)+[^\s:'\"]+")
_SAFE_SECTION_PART = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SAFE_SECTION_NUMBER = re.compile(r"^\d{2}$")
_SECTION_FILE = re.compile(r"^(\d+)_.*\.md$")
_LEVEL_TWO_HEADING = re.compile(r"^##(?!#)\s+(.+?)\s*$")
_HEADING_NUMBER_PREFIX = re.compile(r"^0*(\d+)(?:\s*[.:—-]\s*|\s+)(.+)$")
_REPORT_REFERENCES_HEADING = re.compile(
    r"^##\s+(?:\d+\s*[.:—-]\s*)?references\b",
    re.IGNORECASE,
)
_VERDICTS = frozenset({"strong", "acceptable", "insufficient", "blocked"})
_JUDGE_VERDICT_ARTIFACTS = {
    "06a_coverage_judge.md": ("coverage_judge", "verdict"),
    "06b_scope_judge.md": ("scope_judge", "verdict"),
    "06c_benchmark_judge.md": ("benchmark_judge", "verdict"),
    "06d_gap_judge.md": ("gap_judge", "verdict"),
    "06_judge_panel.md": ("judge_synthesizer", "overall_verdict"),
}


def _strip_inline_markup(value: str) -> str:
    """Remove bounded Markdown/JSON wrappers without accepting surrounding prose."""
    stripped = value.strip()
    while True:
        normalized = stripped.strip("`*_\"'").strip()
        if normalized == stripped:
            return normalized
        stripped = normalized


def _normalize_judge_field(value: str) -> str:
    normalized = _strip_inline_markup(value).casefold()
    return re.sub(r"[\s-]+", "_", normalized)


def _normalize_judge_value(value: str) -> str | None:
    normalized = re.sub(r"\s+#{1,6}\s*$", "", value.strip())
    while True:
        cleaned = _strip_inline_markup(normalized).rstrip(",;.").strip()
        if cleaned == normalized:
            break
        normalized = cleaned
    normalized = normalized.casefold()
    return normalized if normalized in _VERDICTS else None


def _judge_verdict_candidate(line: str, field: str) -> tuple[bool, str | None]:
    """Parse one exact judge field while tolerating common serialization decoration."""
    normalized = unicodedata.normalize("NFKC", line).strip()
    if not normalized:
        return False, None

    if normalized.startswith("|") and normalized.endswith("|"):
        cells = [cell.strip() for cell in normalized[1:-1].split("|")]
        if not cells or _normalize_judge_field(cells[0]) != field:
            return False, None
        if len(cells) != 2:
            return True, None
        return True, _normalize_judge_value(cells[1])

    while True:
        undecorated = re.sub(
            r"^(?:#{1,6}\s+|>{1,3}\s*|(?:[-+*]|\d+[.)])\s+)",
            "",
            normalized,
        )
        if undecorated == normalized:
            break
        normalized = undecorated.strip()
    normalized = re.sub(r"\s+#{1,6}\s*$", "", normalized)
    parts = re.split(r"\s*(?::|=|\u2014|\u2013)\s*", normalized, maxsplit=1)
    if len(parts) != 2 or _normalize_judge_field(parts[0]) != field:
        return False, None
    return True, _normalize_judge_value(parts[1])


def _card_artifact_path(paper_id: object) -> str | None:
    if not isinstance(paper_id, str) or canonicalize_arxiv_id(paper_id) != paper_id:
        return None
    stem = arxiv_artifact_stem(paper_id)
    return f"cards/{stem}.md" if stem is not None else None


def _normalized_section_heading(line: str) -> tuple[int | None, str] | None:
    """Return a comparable section number/title from one Markdown H2 heading."""
    match = _LEVEL_TWO_HEADING.match(line.strip())
    if match is None:
        return None
    heading = re.sub(r"\s+#+\s*$", "", match.group(1)).strip()
    number: int | None = None
    numbered = _HEADING_NUMBER_PREFIX.match(heading)
    if numbered is not None:
        number = int(numbered.group(1))
        heading = numbered.group(2)
    normalized = " ".join(unicodedata.normalize("NFKC", heading).split()).casefold()
    return number, normalized


@dataclass(frozen=True, slots=True)
class ArtifactContract:
    """Diagnostic-only artifact expectations for one existing workflow component."""

    component: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()


ARTIFACT_CONTRACTS = (
    ArtifactContract("anchor", required=("00_survey_spec.md",)),
    ArtifactContract("query_plan", required=("01_query_plan.md",)),
    ArtifactContract("method_scout", required=("02a_method_candidates.md",)),
    ArtifactContract("benchmark_scout", required=("02b_benchmark_candidates.md",)),
    ArtifactContract("survey_scout", required=("02c_survey_candidates.md",)),
    ArtifactContract("frontier_scout", required=("02d_frontier_candidates.md",)),
    ArtifactContract("discovery_merger", required=("02_candidate_pool.md",)),
    ArtifactContract("discovery", required=("02_candidate_pool.md",)),
    ArtifactContract("citation_seed_selector", required=("03a_seed_papers.md",)),
    ArtifactContract("reference_expander", required=("03b_citation_expansion.md",)),
    ArtifactContract("semantic_expander", required=("03c_semantic_expansion.md",)),
    ArtifactContract("cross_domain_expander", required=("03d_cross_domain.md",)),
    ArtifactContract("expansion_merger", required=("03_expansion.md",)),
    ArtifactContract("expansion", required=("03_expansion.md",)),
    ArtifactContract("rank_pool", required=("04_ranked_pool.md",)),
    ArtifactContract("card_plan", required=("00_card_plan.json",)),
    ArtifactContract("research_map", required=("05_research_map.md",)),
    ArtifactContract("coverage_judge", required=("06a_coverage_judge.md",)),
    ArtifactContract("scope_judge", required=("06b_scope_judge.md",)),
    ArtifactContract("benchmark_judge", required=("06c_benchmark_judge.md",)),
    ArtifactContract("gap_judge", required=("06d_gap_judge.md",)),
    ArtifactContract("judge_synthesizer", required=("06_judge_panel.md",)),
    ArtifactContract("judge_panel", required=("06_judge_panel.md",)),
    ArtifactContract("image_planner", optional=("08_global_picture.png",)),
    ArtifactContract(
        "survey_outline",
        required=("00_outline.json", "00_outline.md", "00_sections.json"),
    ),
    ArtifactContract("survey_finalizer", required=("08_survey.md", "index.md")),
)
_CONTRACT_BY_COMPONENT = {contract.component: contract for contract in ARTIFACT_CONTRACTS}
_DURABLE_PLANS = {
    "00_card_plan.json": ("card_plan", "spawn_PaperCard"),
    "00_sections.json": ("survey_outline", "spawn_SectionExpander"),
}
_MILESTONE_COMPONENTS = (
    "anchor",
    "query_plan",
    "discovery_merger",
    "expansion_merger",
    "rank_pool",
    "research_map",
    "judge_synthesizer",
    "survey_outline",
    "survey_finalizer",
)
_PIPELINE_STAGES = (
    "anchor",
    "query_plan",
    "discovery",
    "expansion",
    "rank_pool",
    "card_plan",
    "research_map",
    "judge_panel",
    "image_planner",
    "survey_outline",
    "survey_finalizer",
)
_COMPONENT_STAGE = {
    "method_scout": "discovery",
    "benchmark_scout": "discovery",
    "survey_scout": "discovery",
    "frontier_scout": "discovery",
    "discovery_merger": "discovery",
    "citation_seed_selector": "expansion",
    "reference_expander": "expansion",
    "semantic_expander": "expansion",
    "cross_domain_expander": "expansion",
    "expansion_merger": "expansion",
    "paper_card": "card_plan",
    "coverage_judge": "judge_panel",
    "scope_judge": "judge_panel",
    "benchmark_judge": "judge_panel",
    "gap_judge": "judge_panel",
    "judge_synthesizer": "judge_panel",
    "section_expander": "survey_outline",
}


def _sanitize_string(value: str, *, run_root: Path, key: str | None) -> str:
    if key is not None and _SECRET_KEY.search(key):
        return "<redacted>"
    root = str(run_root.resolve(strict=False))
    run_marker = "SCHOLIGHT_SAFE_RUN_DIR"
    sanitized = value.replace(root, run_marker) if root else value
    sanitized = _SECRET_VALUE.sub(
        lambda match: f"{match.group(1)}<redacted>" if match.group(1) else "<redacted>",
        sanitized,
    )
    sanitized = _ABSOLUTE_PATH.sub("<redacted-path>", sanitized)
    sanitized = sanitized.replace(run_marker, "<run_dir>")
    limit = _MAX_QUERY_LENGTH if key in {"query", "search_query"} else _MAX_STRING_LENGTH
    if len(sanitized) > limit:
        return f"{sanitized[:limit].rstrip()}…"
    return sanitized


def sanitize_diagnostic_value(
    value: object,
    *,
    run_root: Path,
    key: str | None = None,
) -> object:
    """Recursively retain bounded diagnostic values while removing secrets and paths."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_string(value, run_root=run_root, key=key)
    if isinstance(value, dict):
        sanitized: dict[str, object] = {}
        for index, (raw_key, raw_value) in enumerate(value.items()):
            if index >= _MAX_COLLECTION_ITEMS:
                sanitized["<truncated>"] = len(value) - _MAX_COLLECTION_ITEMS
                break
            item_key = str(raw_key)[:128]
            sanitized[item_key] = sanitize_diagnostic_value(
                raw_value,
                run_root=run_root,
                key=item_key,
            )
        return sanitized
    if isinstance(value, (list, tuple)):
        items = [
            sanitize_diagnostic_value(item, run_root=run_root)
            for item in value[:_MAX_COLLECTION_ITEMS]
        ]
        if len(value) > _MAX_COLLECTION_ITEMS:
            items.append(f"<truncated:{len(value) - _MAX_COLLECTION_ITEMS}>")
        return items
    return f"<{type(value).__name__}>"


def sanitize_tool_arguments(
    tool: str,
    arguments: object,
    *,
    run_root: Path,
) -> dict[str, object]:
    """Retain only the small argument subset useful for diagnosing existing tools."""
    if not isinstance(arguments, dict):
        return {}
    normalized = tool.casefold()
    if normalized in {"spawn_papercard", "spawn_sectionexpander"}:
        raw_items = arguments.get("items")
        items = raw_items if isinstance(raw_items, list) else []
        id_fields = ("id",) if normalized == "spawn_papercard" else ("n", "slug")
        item_ids: list[str] = []
        for raw_item in items[:_MAX_COLLECTION_ITEMS]:
            if not isinstance(raw_item, dict):
                continue
            parts = [str(raw_item.get(field, "")) for field in id_fields]
            if all(parts):
                item_ids.append("_".join(parts))
        result: dict[str, object] = {
            "item_count": len(items),
            "item_ids": item_ids,
        }
        if isinstance(arguments.get("max_parallel"), int):
            result["max_parallel"] = arguments["max_parallel"]
        return result
    allowed: tuple[str, ...]
    if "search" in normalized:
        allowed = ("query", "strength", "limit", "offset", "date_from", "date_to")
    elif "arxiv_download" in normalized:
        allowed = ("id", "ids", "arxiv_id", "path", "file_path")
    elif normalized in {"fs", "filesystem"}:
        allowed = ("operation", "path", "file_path", "glob", "recursive")
    elif "image" in normalized:
        allowed = ("filePath", "file_path", "size", "width", "height")
    else:
        allowed = (
            "id",
            "ids",
            "operation",
            "path",
            "file_path",
            "limit",
            "offset",
        )
    retained = {field: arguments[field] for field in allowed if field in arguments}
    sanitized = sanitize_diagnostic_value(retained, run_root=run_root)
    return sanitized if isinstance(sanitized, dict) else {}


def _valid_artifact(run_root: Path, relative_path: str) -> bool:
    candidate = run_root / relative_path
    try:
        candidate_stat = candidate.lstat()
        resolved_root = run_root.resolve(strict=True)
        resolved_candidate = candidate.resolve(strict=True)
    except OSError:
        return False
    return (
        stat.S_ISREG(candidate_stat.st_mode)
        and not candidate.is_symlink()
        and resolved_candidate.is_relative_to(resolved_root)
        and candidate_stat.st_size > 0
    )


class SurveyDiagnostics:
    """Append a safe trace and maintain a small atomic diagnostic checkpoint."""

    def __init__(self, *, run_root: Path, job_id: UUID, survey_id: UUID) -> None:
        self.run_root = run_root
        self.job_id = job_id
        self.survey_id = survey_id
        self._sequence = 0
        self._event_count = 0
        self._write_failure_count = 0
        self._last_event: dict[str, object] | None = None
        self._last_activity_at: str | None = None
        self._last_successful_component: str | None = None
        self._tool_counts = {"started": 0, "finished": 0, "failed": 0}
        self._model_counts = {"started": 0, "finished": 0, "failed": 0}
        self._last_model_error: dict[str, object] | None = None
        self._blocking_model_error: dict[str, object] | None = None
        self._last_image_error: dict[str, object] | None = None
        self._evidence_summary: dict[str, object] | None = None
        self._anomalies: list[dict[str, str]] = []
        self._anomaly_keys: set[tuple[str, str, str]] = set()
        self._observed_artifacts: dict[str, dict[str, object]] = {}
        self._dynamic_required: dict[str, str] = {}

    @property
    def write_failure_count(self) -> int:
        return self._write_failure_count

    def last_activity_age_seconds(self, *, now: datetime | None = None) -> int:
        """Return a low-cardinality staleness gauge without changing activity state."""
        if self._last_activity_at is None:
            return 0
        try:
            last_activity = datetime.fromisoformat(self._last_activity_at)
        except ValueError:
            return 0
        current = now or datetime.now(UTC)
        return max(0, round((current - last_activity).total_seconds()))

    def _write_checkpoint(self) -> None:
        temporary = self.run_root / f".{DIAGNOSTICS_FILE}.tmp"
        payload = json.dumps(self.snapshot(), ensure_ascii=False, separators=(",", ":"))
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.run_root / DIAGNOSTICS_FILE)

    def record(self, event_type: str, **fields: object) -> None:
        """Persist one event; diagnostics are deliberately best effort."""
        now = datetime.now(UTC).isoformat()
        self._sequence += 1
        event = {
            "schema_version": 1,
            "sequence": self._sequence,
            "timestamp": now,
            "job_id": str(self.job_id),
            "survey_id": str(self.survey_id),
            "type": event_type,
            **fields,
        }
        sanitized = sanitize_diagnostic_value(event, run_root=self.run_root)
        if not isinstance(sanitized, dict):
            return
        self._event_count += 1
        self._last_event = sanitized
        self._last_activity_at = now
        if event_type.startswith("tool."):
            status = event_type.partition(".")[2]
            if status in self._tool_counts:
                self._tool_counts[status] += 1
        if event_type.startswith("model."):
            status = event_type.partition(".")[2]
            if status in self._model_counts:
                self._model_counts[status] += 1
            if status == "failed":
                self._last_model_error = {
                    key: sanitized[key]
                    for key in (
                        "component",
                        "error_code",
                        "timeout_seconds",
                        "http_status",
                        "failure_kind",
                        "retryable",
                        "duration_ms",
                    )
                    if key in sanitized
                }
                if self._last_model_error.get("component") != "image_planner":
                    self._blocking_model_error = dict(self._last_model_error)
        if event_type == "tool.failed" and sanitized.get("tool") == "image_gen":
            self._last_image_error = {
                key: sanitized[key]
                for key in ("error_code", "http_status", "retryable", "duration_ms")
                if key in sanitized
            }
        try:
            serialized = json.dumps(sanitized, ensure_ascii=False, separators=(",", ":"))
            with (self.run_root / TRACE_FILE).open("a", encoding="utf-8") as handle:
                handle.write(f"{serialized}\n")
                handle.flush()
            self._write_checkpoint()
        except OSError as exc:
            self._write_failure_count += 1
            logger.warning(
                "survey_diagnostics_write_failed",
                job_id=str(self.job_id),
                error_type=type(exc).__name__,
            )

    def _record_anomaly(
        self,
        *,
        component: str,
        expected_artifact: str,
        kind: str,
        severity: str,
    ) -> None:
        key = (component, expected_artifact, kind)
        if key in self._anomaly_keys:
            return
        self._anomaly_keys.add(key)
        anomaly = {
            "component": component,
            "expected_artifact": expected_artifact,
            "kind": kind,
            "severity": severity,
        }
        self._anomalies.append(anomaly)
        self.record("contract.anomaly", **anomaly)
        logger.warning(
            "survey_contract_anomaly",
            job_id=str(self.job_id),
            component=component,
            expected_artifact=expected_artifact,
            kind=kind,
            severity=severity,
        )

    def _reconcile_resolved_anomalies(self) -> None:
        """Drop provisional artifact alarms that the final filesystem state disproves."""
        retained: list[dict[str, str]] = []
        resolved: list[dict[str, str]] = []
        for anomaly in self._anomalies:
            expected_artifact = anomaly["expected_artifact"]
            kind = anomaly["kind"]
            artifact_now_valid = (
                kind == "required_artifact_missing"
                and "#" not in expected_artifact
                and _valid_artifact(self.run_root, expected_artifact)
            )
            plan_now_valid = (
                kind == "plan_artifact_invalid"
                and expected_artifact in _DURABLE_PLANS
                and self.read_durable_plan(expected_artifact) is not None
            )
            if artifact_now_valid or plan_now_valid:
                resolved.append(anomaly)
            else:
                retained.append(anomaly)
        if not resolved:
            return
        self._anomalies = retained
        self._anomaly_keys = {
            (anomaly["component"], anomaly["expected_artifact"], anomaly["kind"])
            for anomaly in retained
        }
        for anomaly in resolved:
            self.record("contract.anomaly_resolved", **anomaly)
            logger.info(
                "survey_contract_anomaly_resolved",
                job_id=str(self.job_id),
                component=anomaly["component"],
                expected_artifact=anomaly["expected_artifact"],
                kind=anomaly["kind"],
            )

    def component_finished(self, component: str, *, status: str) -> None:
        """Record completion and observe its declared outputs without changing control flow."""
        self.record("component.finished", component=component, status=status)
        contract = _CONTRACT_BY_COMPONENT.get(component)
        missing_required = False
        if status == "completed" and contract is not None:
            for relative_path in contract.required:
                if not _valid_artifact(self.run_root, relative_path):
                    missing_required = True
                    self._record_anomaly(
                        component=component,
                        expected_artifact=relative_path,
                        kind="required_artifact_missing",
                        severity="error",
                    )
        if status == "completed" and not missing_required:
            self._last_successful_component = component
            self._write_checkpoint_best_effort()

    def _register_spawn_outputs(self, tool: str, arguments: object) -> None:
        if not isinstance(arguments, dict):
            return
        raw_items = arguments.get("items")
        if not isinstance(raw_items, list):
            return
        normalized = tool.casefold()
        for raw_item in raw_items[:_MAX_EXPECTED_OUTPUTS]:
            if not isinstance(raw_item, dict):
                continue
            if normalized == "spawn_papercard":
                output = _card_artifact_path(raw_item.get("id"))
                if output is not None:
                    self._dynamic_required[output] = "paper_card"
            elif normalized == "spawn_sectionexpander":
                number = str(raw_item.get("n", ""))
                slug = str(raw_item.get("slug", ""))
                if _SAFE_SECTION_PART.fullmatch(number) and _SAFE_SECTION_PART.fullmatch(slug):
                    self._dynamic_required[f"sections/{number}_{slug}.md"] = "section_expander"

    def read_durable_plan(
        self,
        relative_path: str,
        *,
        accept_archived_run_dir: bool = False,
        max_items: int | None = None,
    ) -> list[dict[str, object]] | None:
        """Return one validated bounded fan-out plan, or None when it is unsafe.

        Historical recovery may accept a stale absolute ``run_dir`` because it
        never dereferences that value and derives every expected artifact from
        validated IDs. Live execution keeps requiring the current run root.
        """
        if relative_path not in _DURABLE_PLANS:
            return None
        candidate = self.run_root / relative_path
        if not _valid_artifact(self.run_root, relative_path):
            return None
        try:
            candidate_stat = candidate.lstat()
            if candidate_stat.st_size > _MAX_PLAN_BYTES:
                return None
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        default_max_items = (
            _MAX_CARD_PLAN_ITEMS
            if relative_path == "00_card_plan.json"
            else _MAX_SECTION_PLAN_ITEMS
        )
        item_limit = default_max_items if max_items is None else max_items
        if not 0 <= item_limit <= _MAX_EXPECTED_OUTPUTS:
            return None
        if not isinstance(payload, list) or len(payload) > item_limit:
            return None

        legacy_card_aliases: dict[str, str] = {}
        if relative_path == "00_sections.json":
            card_plan = self.read_durable_plan(
                "00_card_plan.json",
                accept_archived_run_dir=accept_archived_run_dir,
            )
            alias_candidates: dict[str, set[str]] = {}
            for card in card_plan or []:
                card_id = card.get("id")
                if not isinstance(card_id, str):
                    continue
                artifact_stem = arxiv_artifact_stem(card_id)
                if artifact_stem is not None and artifact_stem != card_id:
                    alias_candidates.setdefault(artifact_stem, set()).add(card_id)
            legacy_card_aliases = {
                alias: next(iter(card_ids))
                for alias, card_ids in alias_candidates.items()
                if len(card_ids) == 1
            }

        validated: list[dict[str, object]] = []
        outputs: set[str] = set()
        for raw_item in payload:
            if not isinstance(raw_item, dict):
                return None
            raw_run_dir = raw_item.get("run_dir")
            if not isinstance(raw_run_dir, str):
                return None
            if raw_run_dir != "." and (
                not Path(raw_run_dir).is_absolute()
                or (
                    not accept_archived_run_dir
                    and Path(raw_run_dir).resolve(strict=False)
                    != self.run_root.resolve(strict=False)
                )
            ):
                return None
            if relative_path == "00_card_plan.json":
                paper_id = raw_item.get("id")
                output = _card_artifact_path(paper_id)
                if output is None:
                    return None
                if not all(
                    isinstance(raw_item.get(field), str) and bool(raw_item[field].strip())
                    for field in ("title", "why")
                ):
                    return None
            else:
                number = raw_item.get("n")
                slug = raw_item.get("slug")
                card_ids = raw_item.get("card_ids")
                if (
                    not isinstance(number, str)
                    or not _SAFE_SECTION_NUMBER.fullmatch(number)
                    or not isinstance(slug, str)
                    or not _SAFE_SECTION_PART.fullmatch(slug)
                    or not all(
                        isinstance(raw_item.get(field), str) and bool(raw_item[field].strip())
                        for field in ("title", "thesis")
                    )
                    or not isinstance(raw_item.get("transfer_angle"), str)
                    or not isinstance(card_ids, list)
                ):
                    return None
                normalized_card_ids: list[str] = []
                for card_id in card_ids:
                    if isinstance(card_id, str) and _card_artifact_path(card_id) is not None:
                        normalized_card_ids.append(card_id)
                        continue
                    canonical_id = (
                        legacy_card_aliases.get(card_id) if isinstance(card_id, str) else None
                    )
                    if canonical_id is None:
                        return None
                    normalized_card_ids.append(canonical_id)
                raw_item = {**raw_item, "card_ids": normalized_card_ids}
                output = f"sections/{number}_{slug}.md"
            if output in outputs:
                return None
            outputs.add(output)
            validated.append(raw_item)
        return validated

    def missing_durable_plan_items(
        self,
        relative_path: str,
        *,
        accept_archived_run_dir: bool = False,
        max_items: int | None = None,
    ) -> tuple[dict[str, object], ...] | None:
        """Return only safely planned items whose expected artifact is absent."""
        items = self.read_durable_plan(
            relative_path,
            accept_archived_run_dir=accept_archived_run_dir,
            max_items=max_items,
        )
        if items is None:
            return None
        missing: list[dict[str, object]] = []
        for item in items:
            if relative_path == "00_card_plan.json":
                output = _card_artifact_path(item.get("id"))
                if output is None:
                    return None
            else:
                output = f"sections/{item['n']}_{item['slug']}.md"
            if not _valid_artifact(self.run_root, output):
                missing.append(item)
        return tuple(missing)

    def _audit_judge_verdicts(self) -> None:
        for relative_path, (component, field) in _JUDGE_VERDICT_ARTIFACTS.items():
            if not _valid_artifact(self.run_root, relative_path):
                continue
            try:
                lines = (self.run_root / relative_path).read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                lines = []
            matches: list[str | None] = []
            for line in lines:
                is_candidate, value = _judge_verdict_candidate(line, field)
                if is_candidate:
                    matches.append(value)
            if len(matches) != 1 or matches[0] not in _VERDICTS:
                self._record_anomaly(
                    component=component,
                    expected_artifact=f"{relative_path}#{field}",
                    kind="judge_verdict_invalid",
                    severity="error",
                )

    def _register_durable_plan(self, relative_path: str) -> bool:
        items = self.read_durable_plan(relative_path)
        if items is None:
            return False
        _component, spawn_tool = _DURABLE_PLANS[relative_path]
        self._register_spawn_outputs(spawn_tool, {"items": items})
        return True

    def tool_event(
        self,
        *,
        tool: str,
        status: str,
        component: object = None,
        arguments: object = None,
        **fields: object,
    ) -> None:
        """Record an allowlisted tool event and remember observable spawn outputs."""
        if status == "started":
            self._register_spawn_outputs(tool, arguments)
        safe_arguments = sanitize_tool_arguments(tool, arguments, run_root=self.run_root)
        self.record(
            f"tool.{status}",
            component=component,
            tool=tool,
            status=status,
            arguments=safe_arguments,
            **fields,
        )
        if status in {"finished", "failed"}:
            log_fields = {
                key: fields[key]
                for key in ("error_code", "http_status", "retryable", "duration_ms")
                if key in fields
            }
            logger.info(
                "survey_tool_finished",
                job_id=str(self.job_id),
                component=component,
                tool=tool,
                status=status,
                **log_fields,
            )

    def model_event(self, *, status: str, **fields: object) -> None:
        """Record completion metadata without retaining model input or output content."""
        self.record(f"model.{status}", status=status, **fields)

    def evidence_summary(
        self,
        *,
        card_count: int,
        counts: dict[str, int],
        reviewed_count: int,
        coverage_percent: float,
    ) -> None:
        """Retain aggregate evidence coverage without paper content."""
        self._evidence_summary = {
            "card_count": card_count,
            "counts": dict(counts),
            "reviewed_count": reviewed_count,
            "coverage_percent": coverage_percent,
        }
        self.record("evidence.audited", status="completed", **self._evidence_summary)

    def _write_checkpoint_best_effort(self) -> None:
        try:
            self._write_checkpoint()
        except OSError as exc:
            self._write_failure_count += 1
            logger.warning(
                "survey_diagnostics_write_failed",
                job_id=str(self.job_id),
                error_type=type(exc).__name__,
            )

    def observe_artifacts(self) -> None:
        """Record artifacts and recover expectations without retaining their contents."""
        for relative_path in _DURABLE_PLANS:
            self._register_durable_plan(relative_path)
        paths = {
            relative_path
            for contract in ARTIFACT_CONTRACTS
            for relative_path in (*contract.required, *contract.optional)
        }
        try:
            paths.update(
                str(path.relative_to(self.run_root))
                for directory in ("cards", "sections")
                for path in (self.run_root / directory).glob("*.md")
            )
        except OSError as exc:
            self._write_failure_count += 1
            logger.warning(
                "survey_diagnostics_write_failed",
                job_id=str(self.job_id),
                error_type=type(exc).__name__,
            )
            return
        for relative_path in sorted(paths):
            if relative_path in self._observed_artifacts:
                continue
            candidate = self.run_root / relative_path
            try:
                candidate_stat = candidate.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(candidate_stat.st_mode) or candidate.is_symlink():
                continue
            metadata = {
                "path": relative_path,
                "size_bytes": candidate_stat.st_size,
                "modified_at": datetime.fromtimestamp(candidate_stat.st_mtime, tz=UTC).isoformat(),
            }
            self._observed_artifacts[relative_path] = metadata
            self.record("artifact.observed", **metadata)

    def _audit_final_report_content(self) -> None:
        report = self.run_root / "08_survey.md"
        if not _valid_artifact(self.run_root, "08_survey.md"):
            return
        expected_sections: dict[int, str | None] = {}
        try:
            for path in (self.run_root / "sections").glob("*.md"):
                match = _SECTION_FILE.fullmatch(path.name)
                if match is not None and _valid_artifact(
                    self.run_root,
                    str(path.relative_to(self.run_root)),
                ):
                    expected_heading = next(
                        (
                            heading
                            for line in path.read_text(encoding="utf-8").splitlines()
                            if (heading := _normalized_section_heading(line)) is not None
                        ),
                        None,
                    )
                    expected_sections[int(match.group(1))] = (
                        expected_heading[1] if expected_heading is not None else None
                    )
            observed_sections: set[int] = set()
            observed_titles: set[str] = set()
            has_references = False
            with report.open(encoding="utf-8") as handle:
                for line in handle:
                    stripped = line.strip()
                    if _REPORT_REFERENCES_HEADING.match(stripped):
                        has_references = True
                        continue
                    section_heading = _normalized_section_heading(stripped)
                    if section_heading is not None:
                        section_number, section_title = section_heading
                        if section_number is not None:
                            observed_sections.add(section_number)
                        if section_title:
                            observed_titles.add(section_title)
        except (OSError, UnicodeError):
            self._record_anomaly(
                component="survey_finalizer",
                expected_artifact="08_survey.md",
                kind="final_report_unreadable",
                severity="error",
            )
            return
        missing_sections = (
            section_number
            for section_number, section_title in expected_sections.items()
            if (
                section_title not in observed_titles
                if section_title is not None
                else section_number not in observed_sections
            )
        )
        for section_number in sorted(missing_sections):
            self._record_anomaly(
                component="survey_finalizer",
                expected_artifact=f"08_survey.md#section-{section_number:02d}",
                kind="section_missing_from_final_report",
                severity="error",
            )
        if expected_sections and not has_references:
            self._record_anomaly(
                component="survey_finalizer",
                expected_artifact="08_survey.md#references",
                kind="references_missing_from_final_report",
                severity="error",
            )

    def finalize_contract_audit(self) -> None:
        """Take a final diagnostic snapshot after the existing workflow has stopped."""
        self.observe_artifacts()
        self._reconcile_resolved_anomalies()
        for relative_path, (component, _spawn_tool) in _DURABLE_PLANS.items():
            if _valid_artifact(self.run_root, relative_path) and not self._register_durable_plan(
                relative_path
            ):
                self._record_anomaly(
                    component=component,
                    expected_artifact=relative_path,
                    kind="plan_artifact_invalid",
                    severity="error",
                )
        for component in _MILESTONE_COMPONENTS:
            contract = _CONTRACT_BY_COMPONENT[component]
            if contract.required and all(
                _valid_artifact(self.run_root, relative_path) for relative_path in contract.required
            ):
                self._last_successful_component = component
        for contract in ARTIFACT_CONTRACTS:
            for relative_path in contract.required:
                if not _valid_artifact(self.run_root, relative_path):
                    self._record_anomaly(
                        component=contract.component,
                        expected_artifact=relative_path,
                        kind="required_artifact_missing",
                        severity="error",
                    )
        self._audit_judge_verdicts()
        self._audit_final_report_content()
        for contract in ARTIFACT_CONTRACTS:
            for relative_path in contract.optional:
                if not _valid_artifact(self.run_root, relative_path):
                    self._record_anomaly(
                        component=contract.component,
                        expected_artifact=relative_path,
                        kind="optional_artifact_missing",
                        severity="warning",
                    )
        for relative_path, component in sorted(self._dynamic_required.items()):
            if not _valid_artifact(self.run_root, relative_path):
                self._record_anomaly(
                    component=component,
                    expected_artifact=relative_path,
                    kind="required_artifact_missing",
                    severity="error",
                )
        self._write_checkpoint_best_effort()

    def finalize_recovery_audit(self) -> None:
        """Audit rebuilt final artifacts without applying newer intermediate contracts.

        Archived runs can predate durable-plan and Judge verdict schemas. Recovery
        proves safety by restoring manifest-verified inputs, rebuilding the final
        artifacts, and comparing their immutable hashes; revalidating historical
        intermediate files against the current workflow schema would create false
        contract failures unrelated to the archived report.
        """
        self.observe_artifacts()
        final_contract = _CONTRACT_BY_COMPONENT["survey_finalizer"]
        for relative_path in final_contract.required:
            if not _valid_artifact(self.run_root, relative_path):
                self._record_anomaly(
                    component=final_contract.component,
                    expected_artifact=relative_path,
                    kind="required_artifact_missing",
                    severity="error",
                )
        self._audit_final_report_content()
        for contract in ARTIFACT_CONTRACTS:
            for relative_path in contract.optional:
                if not _valid_artifact(self.run_root, relative_path):
                    self._record_anomaly(
                        component=contract.component,
                        expected_artifact=relative_path,
                        kind="optional_artifact_missing",
                        severity="warning",
                    )
        self._write_checkpoint_best_effort()

    def snapshot(self) -> dict[str, Any]:
        """Return the bounded summary persisted during execution and copied into run.json."""
        first_anomaly = self._anomalies[0] if self._anomalies else None
        affected_components: list[str] = []
        if first_anomaly is not None and first_anomaly.get("severity") == "error":
            component = first_anomaly.get("component", "")
            stage = _COMPONENT_STAGE.get(component, component)
            if stage in _PIPELINE_STAGES:
                affected_components = list(_PIPELINE_STAGES[_PIPELINE_STAGES.index(stage) + 1 :])
        return {
            "schema_version": 1,
            "job_id": str(self.job_id),
            "survey_id": str(self.survey_id),
            "event_count": self._event_count,
            "write_failure_count": self._write_failure_count,
            "last_activity_at": self._last_activity_at,
            "last_event": self._last_event,
            "last_successful_component": self._last_successful_component,
            "tool_counts": dict(self._tool_counts),
            "model_counts": dict(self._model_counts),
            "last_model_error": self._last_model_error,
            "blocking_model_error": self._blocking_model_error,
            "last_image_error": self._last_image_error,
            "evidence_summary": self._evidence_summary,
            "anomaly_count": len(self._anomalies),
            "first_anomaly": first_anomaly,
            "affected_components": affected_components,
            "anomalies": list(self._anomalies),
            "observed_artifacts": list(self._observed_artifacts.values()),
            "expected_dynamic_artifacts": sorted(self._dynamic_required),
            "trace_path": TRACE_FILE,
        }


__all__ = [
    "ARTIFACT_CONTRACTS",
    "DIAGNOSTICS_FILE",
    "TRACE_FILE",
    "ArtifactContract",
    "SurveyDiagnostics",
    "sanitize_diagnostic_value",
    "sanitize_tool_arguments",
]
