"""Concurrent Scholight Survey execution and durable artifact archiving."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import stat
import sys
import time
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import structlog

from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.queries_survey import (
    SurveyJob,
    claim_survey_job,
    defer_survey_archive,
    finish_survey_archive,
    get_survey,
    heartbeat_survey_job,
    mark_survey_workspace_missing,
    recover_expired_survey_jobs,
    settle_survey_execution,
    update_survey_job_progress,
)
from scholight.db.queries_survey_attempts import heartbeat_compute_attempt
from scholight.db.queries_survey_capacity import get_survey_capacity_snapshot
from scholight.logging.emf import emit_emf
from scholight.sources.arxiv import arxiv_artifact_stem
from scholight.survey.artifacts import SurveyArtifactStore
from scholight.survey.capacity import (
    SurveyCapacityReporter,
    SurveyTaskProtection,
    emit_survey_database_latency,
)
from scholight.survey.cleanup_worker import serve_artifact_cleanup
from scholight.survey.contracts import SurveyLeaseLostError
from scholight.survey.diagnostics import SurveyDiagnostics
from scholight.survey.email_notifications import AliyunSurveyEmailSender
from scholight.survey.evidence import (
    SurveyEvidenceAuditError,
    SurveyEvidenceSummary,
    audit_survey_evidence,
    summarize_survey_evidence,
)
from scholight.survey.extracts import materialize_extracts
from scholight.survey.finalizer import SurveyFinalizationError, finalize_survey
from scholight.survey.metrics import emit_chart_metrics, is_provider_throttled
from scholight.survey.notification_worker import SurveyEmailSender, serve_email_notifications
from scholight.survey.process import (
    ProcessControl,
    classify_rcm_error,
    is_transient_rcm_error,
    provider_retry_delay_seconds,
    read_sanitized_tail,
    terminate_process_group,
    write_stdin,
)
from scholight.survey.progress import stage_for_component
from scholight.survey.rcm_diagnostics import sanitize_completion_failure
from scholight.survey.report_pdf import (
    fallback_title as report_fallback_title,
)
from scholight.survey.runtime import survey_environment
from scholight.survey.workflow_resources import (
    WorkflowResourceError,
    prepare_workflow_workspace,
    stage_workflow_schema,
)
from scholight.survey.workflow_runtime import workflow_file

logger = structlog.get_logger(__name__)

RCM_VERSION = "0.2.23"
WORKFLOW_VERSION = "scholight-survey-v2.1"
_IDLE_SECONDS = 1
_RECOVERY_SECONDS = 30
_EVENT_READ_BYTES = 64 * 1024
_EVENT_LINE_LIMIT = 1024 * 1024
_STAGE_RECORD_LIMIT = 512
_ARTIFACT_OBSERVE_SECONDS = 5
_ACTIVITY_METRIC_SECONDS = 60
_MODEL_TIMEOUT = re.compile(r"(?:timed out after|timeout(?: after)?)\s+(\d+)\s*s", re.I)
_IMAGE_HTTP_STATUS = re.compile(r"\b(?:HTTP\s+|http_status=)([45]\d\d)\b", re.I)
_IMAGE_ERROR_CODE = re.compile(r"\bcode=([a-z0-9_]+)\b", re.I)
_IMAGE_RETRYABLE = re.compile(r"\bretryable=(true|false)\b", re.I)
_FINALIZATION_ERROR_CODES = frozenset(
    {
        "survey_report_missing",
        "survey_artifact_contract_invalid",
        "survey_outline_metadata_invalid",
        "survey_section_contract_invalid",
        "survey_reference_contract_invalid",
        "survey_finalization_write_failed",
        "survey_finalization_output_invalid",
        "survey_report_internal_metadata_leaked",
    }
)
_MODEL_TERMINAL_ERROR_CODES = frozenset(
    {
        "survey_provider_rate_limited",
        "survey_provider_unavailable",
        "survey_model_auth_failed",
        "survey_model_request_rejected",
        "survey_model_configuration_failed",
        "survey_model_completion_failed",
    }
)


def _classify_model_hitch(preview: object, *, role: object = None) -> dict[str, object] | None:
    """Classify an RCM hitch in memory without persisting its model-provided text."""
    if not isinstance(preview, str):
        return None
    normalized = preview.casefold()
    timeout_match = _MODEL_TIMEOUT.search(preview)
    if timeout_match is not None:
        return {
            "error_code": "model_timeout",
            "timeout_seconds": int(timeout_match.group(1)),
            "retryable": True,
        }
    status_match = re.search(r"(?:status|http)\D{0,8}([45]\d\d)", normalized)
    http_status = int(status_match.group(1)) if status_match is not None else None
    if http_status == 429 or "rate limit" in normalized:
        result: dict[str, object] = {
            "error_code": "model_rate_limited",
            "retryable": True,
        }
    elif http_status == 408 or "timed out" in normalized or "timeout" in normalized:
        result = {"error_code": "model_timeout", "retryable": True}
    elif http_status == 425 or (http_status is not None and 500 <= http_status <= 599):
        result = {"error_code": "model_provider_unavailable", "retryable": True}
    elif http_status in {401, 403} or any(
        marker in normalized for marker in ("unauthorized", "authentication", "invalid api key")
    ):
        result = {"error_code": "model_authentication_failed", "retryable": False}
    elif http_status is not None and 400 <= http_status <= 499:
        result = {"error_code": "model_request_rejected", "retryable": False}
    elif any(
        marker in normalized
        for marker in ("connection", "network", "dns", "transport", "error sending request")
    ):
        result = {"error_code": "model_network_failed", "retryable": True}
    elif role == "system":
        result = {"error_code": "model_configuration_failed", "retryable": False}
    else:
        result = {"error_code": "model_completion_failed", "retryable": False}
    if http_status is not None:
        result["http_status"] = http_status
    return result


def _classify_completion_failure(event: dict[str, object]) -> dict[str, object]:
    """Map new RCM completion metadata to stable diagnostics without content."""
    sanitized = sanitize_completion_failure(event)
    failure_kind = sanitized.get("failure_kind")
    declared_kind = failure_kind if isinstance(failure_kind, str) else "unknown"
    kind = declared_kind
    http_status = sanitized.get("http_status")
    status = http_status if isinstance(http_status, int) and 100 <= http_status <= 599 else None
    error_codes = {
        "rate_limited": "model_rate_limited",
        "authentication": "model_authentication_failed",
        "timeout": "model_timeout",
        "provider_unavailable": "model_provider_unavailable",
        "provider_error": "model_provider_unavailable",
        "network": "model_network_failed",
        "invalid_request": "model_request_rejected",
        "http_error": "model_request_rejected",
        "configuration": "model_configuration_failed",
        "unknown": "model_completion_failed",
    }
    if status == 429:
        error_code = "model_rate_limited"
        kind = "rate_limited"
    elif status == 408:
        error_code = "model_timeout"
        kind = "timeout"
    elif status == 425 or (status is not None and 500 <= status <= 599):
        error_code = "model_provider_unavailable"
        kind = "provider_unavailable"
    elif status in {401, 403}:
        error_code = "model_authentication_failed"
        kind = "authentication"
    elif status is not None and 400 <= status <= 499:
        error_code = "model_request_rejected"
        kind = "invalid_request"
    else:
        error_code = error_codes.get(kind, "model_completion_failed")
    result: dict[str, object] = {
        "error_code": error_code,
        "failure_kind": declared_kind if declared_kind in error_codes else "unknown",
    }
    if status is not None:
        result["http_status"] = status
    if isinstance(sanitized.get("retryable"), bool):
        result["retryable"] = sanitized["retryable"]
    duration_ms = sanitized.get("duration_ms")
    if isinstance(duration_ms, int) and duration_ms >= 0:
        result["duration_ms"] = duration_ms
    for field in (
        "request_class",
        "provider_code",
        "provider_type",
        "request_id",
        "serialized_request_bytes",
        "estimated_input_tokens",
        "message_count",
        "tool_definition_count",
        "tool_call_count",
        "tool_result_count",
        "thinking_enabled",
        "reasoning_content_present",
        "reasoning_content_bytes",
        "unmatched_tool_call_count",
        "duplicate_tool_call_count",
    ):
        if field in sanitized:
            result[field] = sanitized[field]
    return result


def _public_model_failure(diagnostics: dict[str, Any]) -> tuple[str, str] | None:
    """Return client-safe Survey semantics for the last failed completion."""
    model_error = diagnostics.get("blocking_model_error")
    if not isinstance(model_error, dict):
        model_error = diagnostics.get("last_model_error")
    if not isinstance(model_error, dict):
        return None
    error_code = model_error.get("error_code")
    if error_code == "model_rate_limited":
        return (
            "survey_provider_rate_limited",
            "The Survey model is temporarily rate limited.",
        )
    if error_code in {"model_timeout", "model_provider_unavailable", "model_network_failed"}:
        return (
            "survey_provider_unavailable",
            "The Survey model is temporarily unavailable.",
        )
    if error_code == "model_authentication_failed":
        return (
            "survey_model_auth_failed",
            "The Survey model could not be authenticated.",
        )
    if error_code == "model_request_rejected":
        return (
            "survey_model_request_rejected",
            "The Survey model rejected the request.",
        )
    if error_code == "model_configuration_failed":
        return (
            "survey_model_configuration_failed",
            "The Survey model is not configured correctly.",
        )
    if isinstance(error_code, str):
        return (
            "survey_model_completion_failed",
            "The Survey model did not complete a required step.",
        )
    return None


def _record_evidence_summary(
    diagnostics: SurveyDiagnostics,
    summary: SurveyEvidenceSummary,
) -> None:
    diagnostics.evidence_summary(
        card_count=summary.card_count,
        counts=summary.counts,
        reviewed_count=summary.reviewed_count,
        coverage_percent=summary.coverage_percent,
    )
    for level, count in summary.counts.items():
        emit_emf(
            service="survey-full-worker",
            outcome=level,
            metrics={"SurveyPaperEvidenceCount": (count, "Count")},
        )
    emit_emf(
        service="survey-full-worker",
        metrics={"SurveyFullTextCoverage": (summary.coverage_percent, "Percent")},
    )


def _classify_image_tool_error(error: object) -> dict[str, object]:
    """Map one provider/tool error to bounded metadata without retaining its text."""
    normalized = error.casefold() if isinstance(error, str) else ""
    status_match = _IMAGE_HTTP_STATUS.search(error) if isinstance(error, str) else None
    http_status = int(status_match.group(1)) if status_match is not None else None
    code_match = _IMAGE_ERROR_CODE.search(error) if isinstance(error, str) else None
    declared_code = code_match.group(1).casefold() if code_match is not None else None
    retryable_match = _IMAGE_RETRYABLE.search(error) if isinstance(error, str) else None
    declared_retryable = (
        retryable_match.group(1).casefold() == "true" if retryable_match is not None else None
    )
    stable_codes = {
        "image_rate_limited",
        "image_authentication_failed",
        "image_provider_unavailable",
        "image_request_rejected",
        "image_response_invalid",
        "image_response_too_large",
        "image_url_rejected",
        "image_configuration_invalid",
        "image_configuration_missing",
    }
    if declared_code in stable_codes:
        code = declared_code
        retryable = bool(declared_retryable)
    elif http_status == 429 or "rate limit" in normalized:
        code = "image_rate_limited"
        retryable = True
    elif http_status in {401, 403} or any(
        marker in normalized for marker in ("unauthorized", "authentication", "invalid api key")
    ):
        code = "image_authentication_failed"
        retryable = False
    elif (
        http_status == 408
        or (http_status is not None and http_status >= 500)
        or any(marker in normalized for marker in ("timed out", "request failed", "connect"))
    ):
        code = "image_provider_unavailable"
        retryable = True
    elif "image_gen_api_key" in normalized and "not set" in normalized:
        code = "image_configuration_missing"
        retryable = False
    elif any(
        marker in normalized
        for marker in ("b64_json", "parse image response", "decode image", "content type")
    ):
        code = "image_response_invalid"
        retryable = False
    elif any(marker in normalized for marker in ("create output directory", "write image")):
        code = "image_artifact_write_failed"
        retryable = False
    else:
        code = "image_generation_failed"
        retryable = False
    result: dict[str, object] = {"error_code": code, "retryable": retryable}
    if http_status is not None:
        result["http_status"] = http_status
    return result


@dataclass(frozen=True, slots=True)
class SurveyRepairContext:
    """Minimal owner identity needed by a bounded repair workflow."""

    id: UUID
    survey_id: UUID
    user_id: int


@dataclass(frozen=True, slots=True)
class SurveyExecutionResult:
    outcome: Literal["succeeded", "failed", "cancelled"]
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime
    stage_timings: tuple[dict[str, object], ...] = ()
    return_code: int | None = None
    termination_reason: str | None = None
    stderr_tail: str | None = None
    diagnostics: dict[str, Any] | None = None
    chargeable: bool = True


class SurveyJobExecutor(Protocol):
    async def __call__(
        self,
        job: SurveyJob,
        run_root: Path,
        *,
        control: ProcessControl,
    ) -> SurveyExecutionResult: ...


def _retained_stderr_tail(
    stderr_tail: str,
    *,
    error_code: str | None,
    diagnostics: dict[str, Any],
) -> str | None:
    """Keep bounded local diagnostics, never provider/model response text."""
    model_error = diagnostics.get("last_model_error")
    if isinstance(model_error, dict) or error_code in {
        "survey_model_auth_failed",
        "survey_model_completion_failed",
        "survey_model_configuration_failed",
        "survey_model_request_rejected",
        "survey_provider_rate_limited",
        "survey_provider_unavailable",
    }:
        return None
    return stderr_tail or None


def _workflow_file() -> Path:
    return workflow_file("survey_pipeline.rcm", mcp_url=settings.survey_mcp_url)


def _job_root(job_id: UUID) -> Path:
    return Path(settings.data_root) / "surveys" / str(job_id)


def _child_environment(*, user_id: int, job_id: UUID) -> dict[str, str]:
    return survey_environment(
        user_id=user_id,
        lifetime_seconds=settings.survey_job_timeout_seconds,
        include_image=True,
        survey_job_id=job_id,
    )


def _valid_final_report(run_root: Path) -> bool:
    report = run_root / "08_survey.md"
    try:
        report_stat = report.lstat()
        resolved_root = run_root.resolve(strict=True)
        resolved_report = report.resolve(strict=True)
    except OSError:
        return False
    valid_path = (
        stat.S_ISREG(report_stat.st_mode)
        and not report.is_symlink()
        and resolved_report.parent == resolved_root
        and report_stat.st_size > 0
    )
    if not valid_path:
        return False
    try:
        return bool(report.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        return False


def _publication_quality_reasons(
    *,
    diagnostics: dict[str, Any],
    evidence_summary: SurveyEvidenceSummary | None,
    evidence_error: SurveyEvidenceAuditError | None,
    finalization_error: SurveyFinalizationError | None,
    model_failure: tuple[str, str] | None,
) -> tuple[str, ...]:
    """Return stable reasons that make a readable report free, never hidden."""
    reasons: set[str] = set()
    if model_failure is not None:
        reasons.add("model_step_incomplete")
    if evidence_error is not None:
        reasons.add("evidence_audit_incomplete")
    if evidence_summary is not None:
        if evidence_summary.card_count == 0 or evidence_summary.coverage_percent < 80:
            reasons.add("evidence_coverage_low")
        if (
            evidence_summary.counts.get("unknown", 0) > 0
            or evidence_summary.invalid_reason_count > 0
            or evidence_summary.runtime_marker_count > 0
        ):
            reasons.add("evidence_declarations_incomplete")
    if finalization_error is not None:
        reasons.add("deterministic_finalization_incomplete")
    raw_anomalies = diagnostics.get("anomalies")
    anomalies = raw_anomalies if isinstance(raw_anomalies, list) else []
    if any(
        isinstance(anomaly, dict)
        and anomaly.get("component") != "image_planner"
        and anomaly.get("severity") in {"warning", "error"}
        for anomaly in anomalies
    ):
        reasons.add("workflow_quality_check_incomplete")
    return tuple(sorted(reasons))


def _local_finalization_inputs_available(run_root: Path) -> bool:
    """Return whether deterministic finalization has its minimum material set."""
    return (
        (run_root / "00_outline.md").is_file()
        and (run_root / "sections").is_dir()
        and (run_root / "cards").is_dir()
    )


def _active_model_component(
    active: dict[tuple[str, str, int], tuple[datetime, float]],
) -> str | None:
    """Return an unambiguous component owner for one model completion."""
    if len(active) != 1:
        return None
    return next(iter(active))[0]


def _optional_model_failure_can_finalize(
    diagnostic_summary: dict[str, Any],
    run_root: Path,
) -> bool:
    """Allow local finalization only for the explicitly optional image planner."""
    model_error = diagnostic_summary.get("last_model_error")
    return (
        diagnostic_summary.get("blocking_model_error") is None
        and isinstance(model_error, dict)
        and model_error.get("component") == "image_planner"
        and _local_finalization_inputs_available(run_root)
    )


async def _collect_stage_timings(
    stream: asyncio.StreamReader | None,
    *,
    job_id: UUID | None = None,
    worker_id: UUID | None = None,
    diagnostics: SurveyDiagnostics | None = None,
) -> tuple[dict[str, object], ...]:
    """Drain RCM events while retaining only bounded, non-content stage metadata."""
    if not isinstance(stream, asyncio.StreamReader):
        return ()
    buffer = bytearray()
    discard_until_newline = False
    active: dict[tuple[str, str, int], tuple[datetime, float]] = {}
    active_tools: dict[str, float] = {}
    records: list[dict[str, object]] = []
    last_progress_stage: str | None = None
    structured_failure_pending = False

    while chunk := await stream.read(_EVENT_READ_BYTES):
        buffer.extend(chunk)
        while True:
            newline = buffer.find(b"\n")
            if newline < 0:
                break
            line = bytes(buffer[:newline])
            del buffer[: newline + 1]
            if discard_until_newline:
                discard_until_newline = False
                continue
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                if diagnostics is not None:
                    diagnostics.record(
                        "run.event_dropped",
                        reason="invalid_json",
                        size_bytes=len(line),
                    )
                continue
            if not isinstance(event, dict):
                if diagnostics is not None:
                    diagnostics.record("run.event_dropped", reason="non_object_json")
                continue
            event_type = event.get("type")
            name = event.get("name")
            kind = event.get("kind")
            index = event.get("index")
            tool_status = {
                "tool_call": "started",
                "tool_start": "started",
                "tool_started": "started",
                "tool_result": "finished",
                "tool_done": "finished",
                "tool_finished": "finished",
                "tool_error": "failed",
                "tool_failed": "failed",
            }.get(event_type if isinstance(event_type, str) else "")
            if diagnostics is not None and event_type == "completion_start":
                structured_failure_pending = False
                diagnostics.model_event(status="started")
                continue
            if diagnostics is not None and event_type == "completion_end":
                completion_fields = {
                    field: event[field]
                    for field in (
                        "fragments",
                        "input_tokens",
                        "output_tokens",
                        "total_tokens",
                        "cached_input_tokens",
                        "cache_creation_input_tokens",
                    )
                    if isinstance(event.get(field), int)
                }
                if event.get("outcome") == "failure":
                    structured_failure_pending = True
                    failure = _classify_completion_failure(event)
                    component = _active_model_component(active)
                    if component is not None:
                        failure["component"] = component
                    diagnostics.model_event(
                        status="failed",
                        **failure,
                        **completion_fields,
                    )
                    failure_kind = str(failure.get("failure_kind", "unknown"))
                    logger.warning(
                        "survey_model_completion_failed",
                        job_id=str(diagnostics.job_id),
                        **failure,
                    )
                    emit_emf(
                        service="survey-full-worker",
                        outcome=failure_kind,
                        metrics={"SurveyModelCompletionFailure": (1, "Count")},
                    )
                else:
                    diagnostics.model_event(status="finished", **completion_fields)
                continue
            if (
                diagnostics is not None
                and event_type in {"appended", "taken", "inserted", "replaced"}
                and event.get("kind") == "hitch"
                and event.get("role") in {"assistant", "system"}
            ):
                if structured_failure_pending:
                    structured_failure_pending = False
                    continue
                classification = _classify_model_hitch(
                    event.get("preview"),
                    role=event.get("role"),
                )
                if classification is not None:
                    component = _active_model_component(active)
                    if component is not None:
                        classification["component"] = component
                    diagnostics.model_event(
                        status="failed",
                        **classification,
                    )
                    continue
            if tool_status is not None:
                tool_name = event.get("tool") or event.get("name")
                tool_name = str(tool_name or "unknown")
                call_id = str(event.get("call_id") or "")
                if tool_status == "started" and call_id:
                    active_tools[call_id] = time.monotonic()
                diagnostic_fields: dict[str, object] = {}
                for field in (
                    "call_id",
                    "duration",
                    "duration_ms",
                    "error_code",
                    "result_len",
                    "retryable",
                ):
                    if field in event:
                        diagnostic_fields[field] = event[field]
                started_tool = active_tools.pop(call_id, None) if tool_status != "started" else None
                if started_tool is not None and "duration_ms" not in diagnostic_fields:
                    diagnostic_fields["duration_ms"] = max(
                        0,
                        round((time.monotonic() - started_tool) * 1000),
                    )
                if tool_name.casefold() == "image_gen":
                    if tool_status == "failed":
                        diagnostic_fields.update(_classify_image_tool_error(event.get("error")))
                    if tool_status in {"finished", "failed"}:
                        emit_emf(
                            service="survey-full-worker",
                            outcome="succeeded" if tool_status == "finished" else "failed",
                            metrics={"SurveyImageGenerationCount": (1, "Count")},
                        )
                if diagnostics is not None:
                    diagnostics.tool_event(
                        tool=tool_name,
                        status=tool_status,
                        component=event.get("component"),
                        arguments=event.get("arguments", event.get("args")),
                        **diagnostic_fields,
                    )
                continue
            if not isinstance(name, str) or not isinstance(kind, str) or not isinstance(index, int):
                if diagnostics is not None:
                    safe_keys = sorted(
                        str(key)
                        for key in event
                        if key not in {"content", "message", "preview", "response"}
                    )
                    diagnostics.record(
                        "run.event_dropped",
                        reason="unsupported_event",
                        rcm_event_type=event_type,
                        retained_keys=safe_keys,
                    )
                continue
            key = (name, kind, index)
            now = datetime.now(UTC)
            if event_type == "component_start":
                active[key] = (now, time.monotonic())
                if diagnostics is not None:
                    diagnostics.record(
                        "component.started",
                        component=name,
                        kind=kind,
                        index=index,
                    )
                progress_stage = stage_for_component(name)
                if (
                    progress_stage is not None
                    and progress_stage != last_progress_stage
                    and job_id is not None
                    and worker_id is not None
                ):
                    try:
                        await update_survey_job_progress(
                            job_id=job_id,
                            worker_id=worker_id,
                            stage=progress_stage,
                        )
                    except DBError as exc:
                        logger.warning(
                            "survey_progress_persist_failed",
                            job_id=str(job_id),
                            error_type=type(exc).__name__,
                        )
                    else:
                        last_progress_stage = progress_stage
            elif event_type in {"component_done", "component_skipped"}:
                started_at, started_monotonic = active.pop(key, (now, time.monotonic()))
                if len(records) < _STAGE_RECORD_LIMIT:
                    records.append(
                        {
                            "name": name,
                            "kind": kind,
                            "status": "completed" if event_type == "component_done" else "skipped",
                            "started_at": started_at.isoformat(),
                            "finished_at": now.isoformat(),
                            "duration_ms": max(
                                0,
                                round((time.monotonic() - started_monotonic) * 1000),
                            ),
                        }
                    )
                if diagnostics is not None:
                    diagnostics.component_finished(
                        name,
                        status="completed" if event_type == "component_done" else "skipped",
                    )
        if len(buffer) > _EVENT_LINE_LIMIT:
            buffer.clear()
            discard_until_newline = True
            if diagnostics is not None:
                diagnostics.record(
                    "run.event_dropped",
                    reason="line_too_large",
                    size_bytes=_EVENT_LINE_LIMIT,
                )
    return tuple(records)


async def _observe_artifacts(
    diagnostics: SurveyDiagnostics,
    *,
    stop: asyncio.Event,
) -> None:
    """Watch only artifact metadata; observer failures never affect RCM execution."""
    next_metric_at = 0.0
    while not stop.is_set():
        diagnostics.observe_artifacts()
        now = time.monotonic()
        if now >= next_metric_at:
            emit_emf(
                service="survey-worker",
                metrics={
                    "SurveyLastActivityAge": (
                        diagnostics.last_activity_age_seconds(),
                        "Seconds",
                    )
                },
            )
            next_metric_at = now + _ACTIVITY_METRIC_SECONDS
        try:
            await asyncio.wait_for(stop.wait(), timeout=_ARTIFACT_OBSERVE_SECONDS)
        except TimeoutError:
            continue


def _emit_result_metrics(result: SurveyExecutionResult) -> None:
    diagnostics = result.diagnostics or {}
    raw_anomalies = diagnostics.get("anomalies")
    anomalies = raw_anomalies if isinstance(raw_anomalies, list) else []
    contract_errors = sum(
        1
        for anomaly in anomalies
        if isinstance(anomaly, dict) and anomaly.get("severity") == "error"
    )
    raw_tool_counts = diagnostics.get("tool_counts")
    tool_counts = raw_tool_counts if isinstance(raw_tool_counts, dict) else {}
    tool_failures = int(tool_counts.get("failed", 0))
    write_failures = int(diagnostics.get("write_failure_count", 0))
    duration_ms = max(0, round((result.finished_at - result.started_at).total_seconds() * 1000))
    last_activity_age = 0
    raw_last_activity = diagnostics.get("last_activity_at")
    if isinstance(raw_last_activity, str):
        try:
            last_activity = datetime.fromisoformat(raw_last_activity)
            last_activity_age = max(0, round((result.finished_at - last_activity).total_seconds()))
        except ValueError:
            pass
    emit_emf(
        service="survey-worker",
        outcome=result.outcome,
        metrics={
            "SurveyJobCount": (1, "Count"),
            "SurveyJobDuration": (duration_ms, "Milliseconds"),
        },
    )
    publication_outcome = (
        "degraded" if result.outcome == "succeeded" and not result.chargeable else result.outcome
    )
    emit_emf(
        service="survey-worker",
        outcome=publication_outcome,
        metrics={"SurveyPublicationCount": (1, "Count")},
    )
    emit_emf(
        service="survey-worker",
        metrics={
            "SurveyContractAnomaly": (contract_errors, "Count"),
            "SurveyFinalizationFailure": (
                1 if result.error_code in _FINALIZATION_ERROR_CODES else 0,
                "Count",
            ),
            "SurveyModelTerminalFailure": (
                1 if result.error_code in _MODEL_TERMINAL_ERROR_CODES else 0,
                "Count",
            ),
            "SurveyFullTextRuntimeFailure": (
                1 if result.error_code == "survey_full_text_runtime_unavailable" else 0,
                "Count",
            ),
            "SurveyRuntimeFailure": (1 if result.outcome == "failed" else 0, "Count"),
            "SurveyProviderThrottled": (
                1 if is_provider_throttled(result.error_code) else 0,
                "Count",
            ),
            "SurveyToolFailure": (tool_failures, "Count"),
            "SurveyDiagnosticsWriteFailure": (write_failures, "Count"),
            "SurveyLastActivityAge": (last_activity_age, "Seconds"),
        },
    )


def _finish_diagnostics(
    diagnostics: SurveyDiagnostics,
    *,
    outcome: str,
    return_code: int | None,
    termination_reason: str,
    audit_contract: bool,
) -> dict[str, Any]:
    diagnostics.record(
        "process.finished",
        status=outcome,
        return_code=return_code,
        termination_reason=termination_reason,
    )
    if audit_contract:
        diagnostics.finalize_contract_audit()
    diagnostics.record("run.finished", outcome=outcome)
    return diagnostics.snapshot()


def _timeout_message(seconds: int) -> str:
    hours = max(1, round(seconds / 3600))
    return f"Survey generation exceeded its {hours}-hour execution window."


async def _execute_survey_once(
    job: SurveyJob,
    run_root: Path,
    *,
    control: ProcessControl | None = None,
) -> SurveyExecutionResult:
    """Run the fixed RCM workflow without retaining unbounded subprocess output."""
    started_at = datetime.now(UTC)
    diagnostics = SurveyDiagnostics(
        run_root=run_root,
        job_id=job.id,
        survey_id=job.survey_id,
    )
    diagnostics.record(
        "run.started",
        component="survey_pipeline",
        rcm_version=RCM_VERSION,
        workflow_version=WORKFLOW_VERSION,
    )
    try:
        workspace_resources = prepare_workflow_workspace(run_root)
        workflow_resources = stage_workflow_schema(run_root)
    except WorkflowResourceError as exc:
        diagnostics.record(
            "workflow.resources_failed",
            status="failed",
            error_type=type(exc).__name__,
        )
        diagnostic_summary = _finish_diagnostics(
            diagnostics,
            outcome="failed",
            return_code=None,
            termination_reason="workflow_resources_unavailable",
            audit_contract=False,
        )
        logger.error(
            "survey_workflow_resources_unavailable",
            job_id=str(job.id),
            error_type=type(exc).__name__,
        )
        return SurveyExecutionResult(
            outcome="failed",
            error_code="survey_workflow_resources_unavailable",
            error_message="Survey workflow resources are unavailable.",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            return_code=None,
            termination_reason="workflow_resources_unavailable",
            diagnostics=diagnostic_summary,
        )
    diagnostics.record(
        "workflow.resources_staged",
        status="completed",
        resource_count=len(workflow_resources) + len(workspace_resources),
    )
    try:
        process = await asyncio.create_subprocess_exec(
            "accelerate",
            "run",
            str(_workflow_file()),
            "--stream",
            "--purpose-stdin",
            "--run-dir",
            str(run_root),
            env=_child_environment(user_id=job.user_id, job_id=job.id),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except Exception as exc:
        diagnostics.record(
            "process.finished",
            status="failed",
            termination_reason="process_start_failed",
            error_type=type(exc).__name__,
        )
        diagnostics.record("run.finished", outcome="failed")
        raise
    control = control or ProcessControl()
    await control.attach(process)
    stage_collector = asyncio.create_task(
        _collect_stage_timings(
            process.stdout,
            job_id=job.id,
            worker_id=job.lease_owner,
            diagnostics=diagnostics,
        )
    )
    artifact_stop = asyncio.Event()
    artifact_observer = asyncio.create_task(_observe_artifacts(diagnostics, stop=artifact_stop))
    extract_stop = asyncio.Event()
    extract_task = asyncio.create_task(
        materialize_extracts(
            run_root,
            stop=extract_stop,
            on_event=lambda kind, fields: diagnostics.record(
                f"extract.{kind.removeprefix('extract_')}", **fields
            ),
        )
    )
    stderr_task = asyncio.create_task(read_sanitized_tail(process.stderr))
    wait_task = asyncio.create_task(process.wait())
    lost_task = asyncio.create_task(control.lease_lost.wait())
    cancel_task = asyncio.create_task(control.cancel_requested.wait())

    async def _complete_process() -> tuple[int, tuple[dict[str, object], ...]]:
        await write_stdin(process, job.approved_draft)
        return_code, stage_timings = await asyncio.gather(wait_task, stage_collector)
        return return_code, stage_timings

    lifecycle_task = asyncio.create_task(_complete_process())
    try:
        done, _pending = await asyncio.wait(
            {lifecycle_task, lost_task, cancel_task},
            timeout=settings.survey_job_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lost_task in done and control.lease_lost.is_set():
            await terminate_process_group(process)
            _finish_diagnostics(
                diagnostics,
                outcome="failed",
                return_code=process.returncode,
                termination_reason="lease_lost",
                audit_contract=False,
            )
            raise SurveyLeaseLostError("Survey execution lease is no longer owned")
        if cancel_task in done and control.cancel_requested.is_set():
            await terminate_process_group(process)
            stage_timings = await stage_collector
            stderr_tail = await stderr_task
            diagnostic_summary = _finish_diagnostics(
                diagnostics,
                outcome="cancelled",
                return_code=process.returncode,
                termination_reason="cancelled",
                audit_contract=False,
            )
            return SurveyExecutionResult(
                outcome="cancelled",
                error_code=None,
                error_message=None,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stage_timings=stage_timings,
                return_code=process.returncode,
                termination_reason="cancelled",
                stderr_tail=stderr_tail or None,
                diagnostics=diagnostic_summary,
            )
        if not done:
            await terminate_process_group(process)
            stage_timings = await stage_collector
            stderr_tail = await stderr_task
            diagnostic_summary = _finish_diagnostics(
                diagnostics,
                outcome="failed",
                return_code=process.returncode,
                termination_reason="timed_out",
                audit_contract=True,
            )
            return SurveyExecutionResult(
                outcome="failed",
                error_code="survey_timed_out",
                error_message=_timeout_message(settings.survey_job_timeout_seconds),
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stage_timings=stage_timings,
                return_code=process.returncode,
                termination_reason="timed_out",
                stderr_tail=stderr_tail or None,
                diagnostics=diagnostic_summary,
            )
        try:
            return_code, stage_timings = await lifecycle_task
        except Exception:
            await terminate_process_group(process)
            raise
        stderr_tail = await stderr_task
        if return_code != 0:
            diagnostic_summary = _finish_diagnostics(
                diagnostics,
                outcome="failed",
                return_code=return_code,
                termination_reason="nonzero_exit",
                audit_contract=True,
            )
            model_failure = _public_model_failure(diagnostic_summary)
            error_code, error_message = (
                model_failure if model_failure is not None else classify_rcm_error(stderr_tail)
            )
            logger.error(
                "survey_rcm_failed",
                job_id=str(job.id),
                return_code=return_code,
                error_code=error_code,
            )
            return SurveyExecutionResult(
                outcome="failed",
                error_code=error_code,
                error_message=error_message,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stage_timings=stage_timings,
                return_code=return_code,
                termination_reason="nonzero_exit",
                stderr_tail=_retained_stderr_tail(
                    stderr_tail,
                    error_code=error_code,
                    diagnostics=diagnostic_summary,
                ),
                diagnostics=diagnostic_summary,
            )
        live_diagnostics = diagnostics.snapshot()
        model_failure = _public_model_failure(live_diagnostics)
        if (
            model_failure is not None
            and not _optional_model_failure_can_finalize(
                live_diagnostics,
                run_root,
            )
            and not (
                _valid_final_report(run_root) or _local_finalization_inputs_available(run_root)
            )
        ):
            error_code, error_message = model_failure
            diagnostic_summary = _finish_diagnostics(
                diagnostics,
                outcome="failed",
                return_code=return_code,
                termination_reason="model_completion_failed",
                audit_contract=True,
            )
            logger.error(
                "survey_rcm_failed",
                job_id=str(job.id),
                return_code=return_code,
                error_code=error_code,
                first_anomaly=diagnostic_summary.get("first_anomaly"),
            )
            return SurveyExecutionResult(
                outcome="failed",
                error_code=error_code,
                error_message=error_message,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stage_timings=stage_timings,
                return_code=return_code,
                termination_reason="model_completion_failed",
                stderr_tail=_retained_stderr_tail(
                    stderr_tail,
                    error_code=error_code,
                    diagnostics=diagnostic_summary,
                ),
                diagnostics=diagnostic_summary,
            )
        evidence_summary: SurveyEvidenceSummary | None = None
        evidence_error: SurveyEvidenceAuditError | None = None
        try:
            evidence_summary = audit_survey_evidence(run_root)
            _record_evidence_summary(diagnostics, evidence_summary)
        except SurveyEvidenceAuditError as exc:
            evidence_error = exc
            diagnostics.record("evidence.failed", status="failed", error_code=exc.code)
            logger.error(
                "survey_full_text_evidence_failed",
                job_id=str(job.id),
                error_code=exc.code,
                invalid_card_count=len(exc.invalid_cards),
            )
        finalization_error: SurveyFinalizationError | None = None
        try:
            if job.lease_owner is not None:
                await update_survey_job_progress(
                    job_id=job.id,
                    worker_id=job.lease_owner,
                    stage="finalizing",
                )
            finalized = finalize_survey(run_root)
            diagnostics.component_finished("survey_finalizer", status="completed")
            diagnostics.record("survey_charts_rendered", count=finalized.chart_count)
            diagnostics.record("survey_chart_rejected", count=finalized.chart_rejected_count)
            emit_chart_metrics(
                chart_count=finalized.chart_count,
                chart_rejected_count=finalized.chart_rejected_count,
            )
            logger.info(
                "survey_report_finalized",
                job_id=str(job.id),
                section_count=finalized.section_count,
                reference_count=finalized.reference_count,
                unverified_reference_count=finalized.unverified_reference_count,
            )
        except SurveyFinalizationError as exc:
            finalization_error = exc
            logger.error(
                "survey_report_finalization_failed",
                job_id=str(job.id),
                error_code=exc.code,
                error_type=type(exc).__name__,
                reason=str(exc),
            )
        if (
            finalization_error is not None
            and finalization_error.code == "survey_report_internal_metadata_leaked"
        ):
            diagnostic_summary = _finish_diagnostics(
                diagnostics,
                outcome="failed",
                return_code=return_code,
                termination_reason="unsafe_report_rejected",
                audit_contract=True,
            )
            return SurveyExecutionResult(
                outcome="failed",
                error_code=finalization_error.code,
                error_message="The Survey report contained internal workflow metadata.",
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stage_timings=stage_timings,
                return_code=return_code,
                termination_reason="unsafe_report_rejected",
                diagnostics=diagnostic_summary,
            )
        if not _valid_final_report(run_root):
            if evidence_error is not None:
                error_code = evidence_error.code
                error_message = str(evidence_error)
                termination_reason = "evidence_audit_failed"
            elif model_failure is not None:
                error_code, error_message = model_failure
                termination_reason = "model_completion_failed"
            else:
                error_code = (
                    finalization_error.code
                    if finalization_error is not None
                    else "survey_finalization_output_invalid"
                )
                error_message = (
                    "Survey research finished, but the final report could not be assembled."
                )
                termination_reason = "finalization_failed"
            diagnostic_summary = _finish_diagnostics(
                diagnostics,
                outcome="failed",
                return_code=return_code,
                termination_reason=termination_reason,
                audit_contract=True,
            )
            logger.error(
                "survey_rcm_failed",
                job_id=str(job.id),
                return_code=return_code,
                error_code=error_code,
                first_anomaly=diagnostic_summary.get("first_anomaly"),
            )
            return SurveyExecutionResult(
                outcome="failed",
                error_code=error_code,
                error_message=error_message,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stage_timings=stage_timings,
                return_code=return_code,
                termination_reason=termination_reason,
                stderr_tail=_retained_stderr_tail(
                    stderr_tail,
                    error_code=error_code,
                    diagnostics=diagnostic_summary,
                ),
                diagnostics=diagnostic_summary,
            )
        if model_failure is not None:
            logger.warning(
                "survey_model_failure_finalized_locally",
                job_id=str(job.id),
                error_code=model_failure[0],
            )
        diagnostics.finalize_contract_audit()
        audited_diagnostics = diagnostics.snapshot()
        raw_anomalies = audited_diagnostics.get("anomalies")
        anomalies = raw_anomalies if isinstance(raw_anomalies, list) else []
        contract_errors = [
            anomaly
            for anomaly in anomalies
            if isinstance(anomaly, dict) and anomaly.get("severity") == "error"
        ]
        if contract_errors:
            logger.warning(
                "survey_report_published_with_contract_warnings",
                job_id=str(job.id),
                contract_error_count=len(contract_errors),
            )
        quality_reasons = _publication_quality_reasons(
            diagnostics=audited_diagnostics,
            evidence_summary=evidence_summary,
            evidence_error=evidence_error,
            finalization_error=finalization_error,
            model_failure=(
                model_failure
                if isinstance(audited_diagnostics.get("blocking_model_error"), dict)
                else None
            ),
        )
        if quality_reasons:
            diagnostics.record(
                "publication.degraded",
                status="warning",
                reason_count=len(quality_reasons),
                reasons=list(quality_reasons),
            )
            logger.warning(
                "survey_report_published_degraded",
                job_id=str(job.id),
                reason_count=len(quality_reasons),
                reasons=quality_reasons,
            )
        diagnostic_summary = _finish_diagnostics(
            diagnostics,
            outcome="succeeded",
            return_code=return_code,
            termination_reason="completed_degraded" if quality_reasons else "completed",
            audit_contract=False,
        )
        return SurveyExecutionResult(
            outcome="succeeded",
            error_code="survey_quality_degraded" if quality_reasons else None,
            error_message=(
                "This report was delivered with incomplete quality checks and was not counted "
                "against your Survey allowance."
                if quality_reasons
                else None
            ),
            started_at=started_at,
            finished_at=datetime.now(UTC),
            stage_timings=stage_timings,
            return_code=return_code,
            termination_reason="completed_degraded" if quality_reasons else "completed",
            stderr_tail=(
                stderr_tail if stderr_tail and diagnostic_summary.get("anomaly_count", 0) else None
            ),
            diagnostics=diagnostic_summary,
            chargeable=not quality_reasons,
        )
    except asyncio.CancelledError:
        await terminate_process_group(process)
        raise
    finally:
        artifact_stop.set()
        extract_stop.set()
        lost_task.cancel()
        if process.returncode is None:
            await terminate_process_group(process)
        for task in (
            lifecycle_task,
            wait_task,
            stage_collector,
            stderr_task,
            lost_task,
            cancel_task,
            artifact_observer,
            extract_task,
        ):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            lifecycle_task,
            wait_task,
            stage_collector,
            stderr_task,
            lost_task,
            cancel_task,
            artifact_observer,
            extract_task,
            return_exceptions=True,
        )


def _invalid_evidence_repair_items(
    diagnostics: SurveyDiagnostics,
    invalid_cards: tuple[str, ...],
) -> tuple[dict[str, object], ...] | None:
    """Map invalid card artifacts back to their immutable, validated plan items."""
    if not invalid_cards or len(invalid_cards) > 100:
        return None
    requested = set(invalid_cards)
    if len(requested) != len(invalid_cards) or any(
        Path(path).parts != ("cards", Path(path).name) or not Path(path).name.endswith(".md")
        for path in requested
    ):
        return None
    plan = diagnostics.read_durable_plan("00_card_plan.json")
    if plan is None:
        return None
    selected: list[dict[str, object]] = []
    matched: set[str] = set()
    for item in plan:
        paper_id = item.get("id")
        stem = arxiv_artifact_stem(paper_id) if isinstance(paper_id, str) else None
        artifact = f"cards/{stem}.md" if stem is not None else None
        if artifact in requested:
            selected.append(item)
            matched.add(artifact)
    return tuple(selected) if matched == requested else None


def _repair_workflows(diagnostics: SurveyDiagnostics) -> tuple[tuple[str, str], ...]:
    """Select bounded repair graphs only when a valid durable plan has gaps."""
    selected: list[tuple[str, str]] = []
    for plan, workflow in (
        ("00_card_plan.json", "card_repair.rcm"),
        ("00_sections.json", "section_repair.rcm"),
    ):
        missing = diagnostics.missing_durable_plan_items(plan)
        if missing:
            selected.append((plan, workflow))
    return tuple(selected)


async def _run_repair_workflow(
    *,
    job: SurveyJob | SurveyRepairContext,
    run_root: Path,
    plan: str,
    workflow: str,
    control: ProcessControl,
    invalid_evidence_items: tuple[dict[str, object], ...] = (),
) -> bool:
    """Run one bounded repair graph without replaying upstream research."""
    process = await asyncio.create_subprocess_exec(
        "accelerate",
        "run",
        str(workflow_file(workflow, mcp_url=settings.survey_mcp_url)),
        "--stream",
        "--purpose-stdin",
        "--run-dir",
        str(run_root),
        env=_child_environment(user_id=job.user_id, job_id=job.id),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    await control.attach(process)
    collector = asyncio.create_task(_collect_stage_timings(process.stdout))
    stderr_task = asyncio.create_task(read_sanitized_tail(process.stderr))
    try:
        repair_request: dict[str, object] = {
            "run_dir": ".",
            "repair": "missing_only",
            "plan": plan,
        }
        if invalid_evidence_items:
            repair_request["repair"] = "invalid_evidence"
            repair_request["items"] = invalid_evidence_items
        await write_stdin(
            process,
            json.dumps(repair_request, separators=(",", ":"), sort_keys=True),
        )
        try:
            return_code = await asyncio.wait_for(
                process.wait(),
                timeout=settings.survey_job_timeout_seconds,
            )
        except TimeoutError:
            await terminate_process_group(process)
            logger.warning(
                "survey_artifact_repair_failed",
                job_id=str(job.id),
                plan=plan,
                error_code="survey_repair_timed_out",
            )
            return False
        await collector
        stderr_tail = await stderr_task
        if control.lease_lost.is_set():
            raise SurveyLeaseLostError("Survey execution lease is no longer owned")
        if control.cancel_requested.is_set():
            return False
        if return_code != 0:
            error_code, _message = classify_rcm_error(stderr_tail)
            logger.warning(
                "survey_artifact_repair_failed",
                job_id=str(job.id),
                plan=plan,
                error_code=error_code,
            )
            return False
        return True
    finally:
        if process.returncode is None:
            await terminate_process_group(process)
        for task in (collector, stderr_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(collector, stderr_task, return_exceptions=True)


async def _repair_survey_artifacts(
    job: SurveyJob,
    run_root: Path,
    *,
    original: SurveyExecutionResult,
    control: ProcessControl,
) -> SurveyExecutionResult | None:
    diagnostics = SurveyDiagnostics(
        run_root=run_root,
        job_id=job.id,
        survey_id=job.survey_id,
    )
    repairs: tuple[tuple[str, str, tuple[dict[str, object], ...]], ...]
    if original.error_code == "survey_quality_degraded":
        repairs = ()
        evidence_summary = summarize_survey_evidence(run_root)
        selected = _invalid_evidence_repair_items(
            diagnostics,
            evidence_summary.invalid_cards,
        )
        if not selected:
            return None
        repairs = (("00_card_plan.json", "card_repair.rcm", selected),)
    else:
        repairs = tuple((*repair, ()) for repair in _repair_workflows(diagnostics))
        if original.error_code == "survey_full_text_evidence_invalid":
            evidence_summary = summarize_survey_evidence(run_root)
            if evidence_summary.invalid_cards:
                selected = _invalid_evidence_repair_items(
                    diagnostics,
                    evidence_summary.invalid_cards,
                )
                if not selected:
                    return None
                repairs = (*repairs, ("00_card_plan.json", "card_repair.rcm", selected))
    if not repairs:
        return None
    logger.warning(
        "survey_artifact_repair_scheduled",
        job_id=str(job.id),
        repair_count=len(repairs),
        plans=[plan for plan, _workflow, _items in repairs],
        repair_kinds=["invalid_evidence" if items else "missing_only" for _, _, items in repairs],
        invalid_evidence_item_count=sum(len(items) for _, _, items in repairs),
    )
    emit_emf(
        service="survey-full-worker",
        metrics={"SurveyArtifactRepair": (1, "Count")},
    )
    for plan, workflow, invalid_evidence_items in repairs:
        if control.lease_lost.is_set():
            raise SurveyLeaseLostError("Survey execution lease is no longer owned")
        if control.cancel_requested.is_set():
            return replace(
                original,
                outcome="cancelled",
                error_code=None,
                error_message=None,
                finished_at=datetime.now(UTC),
                termination_reason="cancelled_before_artifact_repair",
            )
        if not await _run_repair_workflow(
            job=job,
            run_root=run_root,
            plan=plan,
            workflow=workflow,
            control=control,
            invalid_evidence_items=invalid_evidence_items,
        ):
            return None

    try:
        evidence_summary = audit_survey_evidence(run_root)
        _record_evidence_summary(diagnostics, evidence_summary)
        finalized = finalize_survey(run_root)
    except (SurveyEvidenceAuditError, SurveyFinalizationError) as exc:
        logger.warning(
            "survey_artifact_repair_failed",
            job_id=str(job.id),
            error_code=exc.code,
        )
        return None
    diagnostics.component_finished("survey_finalizer", status="completed")
    diagnostics.record("survey_charts_rendered", count=finalized.chart_count)
    diagnostics.record("survey_chart_rejected", count=finalized.chart_rejected_count)
    emit_chart_metrics(
        chart_count=finalized.chart_count,
        chart_rejected_count=finalized.chart_rejected_count,
    )
    diagnostics.finalize_contract_audit()
    snapshot = diagnostics.snapshot()
    quality_reasons = _publication_quality_reasons(
        diagnostics=snapshot,
        evidence_summary=evidence_summary,
        evidence_error=None,
        finalization_error=None,
        model_failure=None,
    )
    logger.info(
        "survey_artifact_repair_finished",
        job_id=str(job.id),
        section_count=finalized.section_count,
        reference_count=finalized.reference_count,
    )
    return SurveyExecutionResult(
        outcome="succeeded",
        error_code="survey_quality_degraded" if quality_reasons else None,
        error_message=(
            "This report was delivered with incomplete quality checks and was not counted "
            "against your Survey allowance."
            if quality_reasons
            else None
        ),
        started_at=original.started_at,
        finished_at=datetime.now(UTC),
        stage_timings=original.stage_timings,
        return_code=0,
        termination_reason=(
            "targeted_artifact_repair_degraded" if quality_reasons else "targeted_artifact_repair"
        ),
        diagnostics=snapshot,
        chargeable=not quality_reasons,
    )


async def execute_survey(
    job: SurveyJob,
    run_root: Path,
    *,
    control: ProcessControl | None = None,
) -> SurveyExecutionResult:
    """Run Survey with bounded provider retries and targeted artifact repair."""
    control = control or ProcessControl()
    provider_attempt = 1
    while provider_attempt <= settings.survey_provider_max_attempts:
        result = await _execute_survey_once(job, run_root, control=control)
        if (
            result.outcome == "succeeded"
            and result.error_code == "survey_quality_degraded"
            and summarize_survey_evidence(run_root).invalid_cards
        ):
            repaired = await _repair_survey_artifacts(
                job,
                run_root,
                original=result,
                control=control,
            )
            return repaired or result
        if (
            result.outcome == "failed"
            and result.error_code
            in {"survey_contract_violation", "survey_full_text_evidence_invalid"}
            and result.return_code == 0
        ):
            repaired = await _repair_survey_artifacts(
                job,
                run_root,
                original=result,
                control=control,
            )
            return repaired or result
        if (
            result.outcome != "failed"
            or not is_transient_rcm_error(result.error_code)
            or provider_attempt >= settings.survey_provider_max_attempts
        ):
            return result
        delay = provider_retry_delay_seconds(
            provider_attempt,
            base=settings.survey_provider_retry_base_seconds,
            maximum=settings.survey_provider_retry_max_seconds,
        )
        logger.warning(
            "survey_provider_retry_scheduled",
            job_id=str(job.id),
            attempt=provider_attempt,
            next_attempt=provider_attempt + 1,
            delay_seconds=delay,
            error_code=result.error_code,
        )
        emit_emf(
            service="survey-full-worker",
            metrics={"SurveyProviderRetry": (1, "Count")},
        )
        if control.lease_lost.is_set():
            raise SurveyLeaseLostError("Survey execution lease is no longer owned")
        if control.cancel_requested.is_set():
            return SurveyExecutionResult(
                outcome="cancelled",
                error_code=None,
                error_message=None,
                started_at=result.started_at,
                finished_at=datetime.now(UTC),
                termination_reason="cancelled_before_retry",
            )
        await asyncio.sleep(delay)
        if control.lease_lost.is_set():
            raise SurveyLeaseLostError("Survey execution lease is no longer owned")
        if control.cancel_requested.is_set():
            return SurveyExecutionResult(
                outcome="cancelled",
                error_code=None,
                error_message=None,
                started_at=result.started_at,
                finished_at=datetime.now(UTC),
                termination_reason="cancelled_before_retry",
            )
        shutil.rmtree(run_root, ignore_errors=True)
        run_root.mkdir(parents=True, exist_ok=True)
        provider_attempt += 1
    raise AssertionError("unreachable")


async def _heartbeat(
    *,
    job_id: UUID,
    worker_id: UUID,
    stop: asyncio.Event,
    control: ProcessControl,
    attempt_id: UUID | None = None,
) -> None:
    last_owned = time.monotonic()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.survey_heartbeat_seconds)
            return
        except TimeoutError:
            started_at = time.perf_counter()
            try:
                if attempt_id is None:
                    state = await heartbeat_survey_job(
                        job_id=job_id,
                        worker_id=worker_id,
                        lease_seconds=settings.survey_lease_seconds,
                    )
                else:
                    state = await heartbeat_compute_attempt(
                        attempt_id=attempt_id,
                        lease_seconds=settings.survey_lease_seconds,
                    )
            except Exception as exc:
                logger.warning(
                    "survey_heartbeat_failed",
                    job_id=str(job_id),
                    error_type=type(exc).__name__,
                )
                if time.monotonic() - last_owned < settings.survey_lease_seconds:
                    continue
                state = "lost"
            finally:
                emit_survey_database_latency(
                    queue="survey",
                    service="survey-full-worker",
                    operation="heartbeat",
                    started_at=started_at,
                )
            if state == "owned":
                last_owned = time.monotonic()
                continue
            if state == "cancel_requested":
                logger.info("survey_cancellation_observed", job_id=str(job_id))
                await control.request_cancel()
                return
            if state == "lost" or time.monotonic() - last_owned >= settings.survey_lease_seconds:
                logger.warning("survey_heartbeat_lease_lost", job_id=str(job_id))
                await control.lose_lease()
                return


def _run_metadata(job: SurveyJob, result: SurveyExecutionResult | None) -> dict[str, object]:
    readable = bool(
        (result is not None and result.outcome == "succeeded")
        or job.terminal_outcome == "succeeded"
    )
    chargeable = (
        result.chargeable
        if result is not None
        else job.terminal_outcome == "succeeded" and job.error_code != "survey_quality_degraded"
    )
    return {
        "schema_version": 2,
        "job_id": str(job.id),
        "survey_id": str(job.survey_id),
        "user_id": job.user_id,
        "approved_draft_id": str(job.approved_draft_id),
        "approved_draft_revision": job.approved_draft_revision,
        "outcome": job.terminal_outcome or (result.outcome if result else "failed"),
        "error_code": job.error_code or (result.error_code if result else None),
        "rcm_version": RCM_VERSION,
        "workflow_version": WORKFLOW_VERSION,
        "publication": {
            "readable": readable,
            "chargeable": chargeable,
        },
        "started_at": (
            result.started_at.isoformat()
            if result is not None
            else (job.started_at.isoformat() if job.started_at else None)
        ),
        "finished_at": result.finished_at.isoformat() if result is not None else None,
        "stage_timings": list(result.stage_timings) if result is not None else [],
        "process": {
            "return_code": result.return_code if result is not None else None,
            "termination_reason": result.termination_reason if result is not None else None,
            "stderr_tail": result.stderr_tail if result is not None else None,
        },
        "diagnostics": result.diagnostics if result is not None else None,
    }


def _draft_heading_title(draft_markdown: str) -> str:
    for line in draft_markdown.splitlines():
        text = line.lstrip("#").strip()
        if text:
            return text[:96]
    return "Untitled survey"


async def _report_pdf_title(job: SurveyJob) -> str:
    """Prefer the stored Survey title; fall back to the draft's first heading."""
    try:
        survey = await get_survey(survey_id=job.survey_id, user_id=job.user_id)
    except DBError:
        survey = None
    if survey is not None:
        return survey.title or report_fallback_title(survey.initial_request)
    return _draft_heading_title(job.approved_draft)


async def _prerender_report_pdf(*, job: SurveyJob, run_root: Path) -> None:
    """Best-effort branded PDF render so downloads stream a finished file.

    A production report renders for well over the API gateway's idle timeout,
    so the PDF is produced once at archive time and shipped from the manifest.
    Any failure only logs — archiving must never be blocked by PDF rendering.
    """
    report = run_root / "08_survey.md"
    if not report.is_file():
        return
    try:
        title = await _report_pdf_title(job)
        await _render_report_pdf_child(
            title=title,
            report=report,
            output=run_root / "08_survey.pdf",
            run_root=run_root,
            generated_on=datetime.now(UTC).date(),
        )
        size = (run_root / "08_survey.pdf").stat().st_size
        logger.info("survey_report_pdf_prerendered", job_id=str(job.id), size=size)
    except Exception as exc:
        logger.warning(
            "survey_report_pdf_prerender_failed",
            job_id=str(job.id),
            error_type=type(exc).__name__,
        )


async def _render_report_pdf_child(
    *,
    title: str,
    report: Path,
    output: Path,
    run_root: Path,
    generated_on: date,
) -> None:
    """Render one PDF in a fresh process and keep user content off argv."""
    request_root = run_root.parent / f".pdf-render-{uuid4().hex}"
    request_root.mkdir(mode=0o700)
    request_path = request_root / "request.json"
    request_path.write_text(
        json.dumps(
            {
                "title": title,
                "generated_on": generated_on.isoformat(),
                "asset_root": str(run_root.resolve()),
                "markdown_path": str(report.resolve()),
                "output_path": str(output.resolve()),
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "scholight.survey.pdf_child",
            "--request",
            str(request_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            return_code = await asyncio.wait_for(
                process.wait(),
                timeout=settings.survey_pdf_timeout_seconds,
            )
        except TimeoutError:
            await terminate_process_group(process)
            raise RuntimeError("Survey PDF child timed out") from None
        if return_code != 0 or not output.is_file():
            output.unlink(missing_ok=True)
            raise RuntimeError("Survey PDF child failed")
    finally:
        shutil.rmtree(request_root, ignore_errors=True)


async def _archive(
    *,
    job: SurveyJob,
    worker_id: UUID,
    run_root: Path,
    artifact_store: SurveyArtifactStore,
    result: SurveyExecutionResult | None,
) -> None:
    if not run_root.is_dir():
        job = await mark_survey_workspace_missing(job_id=job.id, worker_id=worker_id)
        run_root.mkdir(parents=True, exist_ok=True)
    await _prerender_report_pdf(job=job, run_root=run_root)
    logger.info("survey_archive_started", job_id=str(job.id))
    try:
        archive = await artifact_store.archive_run(
            user_id=job.user_id,
            job_id=job.id,
            run_root=run_root,
            run_metadata=_run_metadata(job, result),
        )
        await finish_survey_archive(
            job_id=job.id,
            worker_id=worker_id,
            storage_bucket=settings.survey_s3_bucket,
            storage_prefix=archive.storage_prefix,
            manifest_key=archive.manifest_key,
        )
        logger.info(
            "survey_archive_finished",
            job_id=str(job.id),
            manifest_key=archive.manifest_key,
        )
    except SurveyLeaseLostError:
        raise
    except Exception as exc:
        delay_seconds = min(3600, 30 * (2 ** min(job.archive_attempts, 7)))
        await defer_survey_archive(
            job_id=job.id,
            worker_id=worker_id,
            retry_after=timedelta(seconds=delay_seconds),
            error_code="survey_archive_pending",
            error_message="Survey artifacts are still being archived and will be retried.",
        )
        logger.error(
            "survey_archive_deferred",
            job_id=str(job.id),
            error_type=type(exc).__name__,
        )
        return
    try:
        shutil.rmtree(_job_root(job.id))
    except OSError as exc:
        logger.warning(
            "survey_workspace_cleanup_deferred",
            job_id=str(job.id),
            error_type=type(exc).__name__,
        )
    logger.info(
        "survey_job_finished",
        job_id=str(job.id),
        outcome=job.terminal_outcome or (result.outcome if result else "failed"),
    )


async def process_survey_job(
    *,
    job: SurveyJob,
    worker_id: UUID,
    artifact_store: SurveyArtifactStore,
    attempt_id: UUID | None = None,
    execute_job: SurveyJobExecutor | None = None,
) -> None:
    """Execute a pending claim or resume archiving without rerunning RCM."""
    run_root = _job_root(job.id) / "run"
    result: SurveyExecutionResult | None = None
    stop = asyncio.Event()
    control = ProcessControl()
    heartbeat = asyncio.create_task(
        _heartbeat(
            job_id=job.id,
            worker_id=worker_id,
            stop=stop,
            control=control,
            attempt_id=attempt_id,
        )
    )
    try:
        if job.status == "running":
            run_root.mkdir(parents=True, exist_ok=True)
            try:
                executor = execute_job or execute_survey
                result = await executor(job, run_root, control=control)
            except SurveyLeaseLostError:
                logger.info("survey_stopped_after_lease_loss", job_id=str(job.id))
                return
            except Exception as exc:
                now = datetime.now(UTC)
                result = SurveyExecutionResult(
                    outcome="failed",
                    error_code="survey_runtime_unavailable",
                    error_message="Survey generation could not be completed.",
                    started_at=job.started_at or now,
                    finished_at=now,
                )
                logger.error(
                    "survey_execution_failed",
                    job_id=str(job.id),
                    error_type=type(exc).__name__,
                )
            if control.lease_lost.is_set():
                return
            _emit_result_metrics(result)
            logger.info(
                "survey_process_finished",
                job_id=str(job.id),
                outcome=result.outcome,
                error_code=result.error_code,
                return_code=result.return_code,
                termination_reason=result.termination_reason,
            )
            job = await settle_survey_execution(
                job_id=job.id,
                worker_id=worker_id,
                outcome=result.outcome,
                error_code=result.error_code,
                error_message=result.error_message,
                chargeable=result.chargeable,
            )
        if control.lease_lost.is_set():
            return
        await _archive(
            job=job,
            worker_id=worker_id,
            run_root=run_root,
            artifact_store=artifact_store,
            result=result,
        )
    except SurveyLeaseLostError:
        logger.info("survey_stopped_after_lease_loss", job_id=str(job.id))
    except asyncio.CancelledError:
        await control.lose_lease()
        raise
    finally:
        stop.set()
        await heartbeat


async def serve_survey_worker(*, email_sender: SurveyEmailSender | None = None) -> None:
    """Supervise bounded concurrent Surveys with independent leases and process groups."""
    artifact_store = SurveyArtifactStore(
        bucket=settings.survey_s3_bucket,
        endpoint_url=settings.survey_s3_endpoint_url,
    )
    active: set[asyncio.Task[None]] = set()
    protection = SurveyTaskProtection(service="survey-full-worker")
    capacity_reporter = SurveyCapacityReporter(
        queue="survey",
        service="survey-full-worker",
        per_user_concurrency=settings.survey_job_per_user_concurrency,
    )
    cleanup_supervisor = asyncio.create_task(serve_artifact_cleanup())
    if email_sender is None:
        email_sender = AliyunSurveyEmailSender(
            access_key_id=settings.aliyun_dm_access_key_id,
            access_key_secret=settings.aliyun_dm_access_key_secret,
            account_name=settings.aliyun_dm_account_name,
            from_alias=settings.aliyun_dm_from_alias,
            reply_to_address=settings.aliyun_dm_reply_to_address,
        )
    email_supervisor = asyncio.create_task(serve_email_notifications(email_sender))
    last_recovery = 0.0
    logger.info(
        "survey_supervisor_started",
        rcm_version=RCM_VERSION,
        global_concurrency=settings.survey_job_global_concurrency,
        worker_concurrency=settings.survey_job_worker_concurrency,
        per_user_concurrency=settings.survey_job_per_user_concurrency,
        heartbeat_seconds=settings.survey_heartbeat_seconds,
        lease_seconds=settings.survey_lease_seconds,
    )
    try:
        while True:
            active = {task for task in active if not task.done()}
            await capacity_reporter.emit_if_due()
            if cleanup_supervisor.done():
                try:
                    cleanup_supervisor.result()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("survey_cleanup_supervisor_restarted")
                cleanup_supervisor = asyncio.create_task(serve_artifact_cleanup())
            if email_supervisor.done():
                try:
                    email_supervisor.result()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("survey_email_notification_supervisor_restarted")
                email_supervisor = asyncio.create_task(serve_email_notifications(email_sender))
            now = time.monotonic()
            if now - last_recovery >= _RECOVERY_SECONDS:
                try:
                    await recover_expired_survey_jobs()
                except Exception:
                    logger.exception("survey_recovery_cycle_failed")
                last_recovery = now
            claimed = False
            claims_allowed = await protection.ensure() if active else not protection.enabled
            if not active and protection.enabled:
                try:
                    capacity = await get_survey_capacity_snapshot(
                        queue="survey",
                        per_user_concurrency=settings.survey_job_per_user_concurrency,
                    )
                except Exception:
                    logger.exception("survey_claim_preflight_failed")
                else:
                    if capacity.queued > 0:
                        claims_allowed = await protection.ensure()
                    else:
                        await protection.release()
            while claims_allowed and len(active) < settings.survey_job_worker_concurrency:
                worker_id = uuid4()
                started_at = time.perf_counter()
                try:
                    job = await claim_survey_job(
                        worker_id=worker_id,
                        lease_seconds=settings.survey_lease_seconds,
                        global_concurrency=settings.survey_job_global_concurrency,
                        per_user_concurrency=settings.survey_job_per_user_concurrency,
                    )
                except Exception:
                    logger.exception("survey_claim_cycle_failed")
                    break
                finally:
                    emit_survey_database_latency(
                        queue="survey",
                        service="survey-full-worker",
                        operation="claim",
                        started_at=started_at,
                    )
                if job is None:
                    break
                task = asyncio.create_task(
                    _run_claimed_job(
                        job=job,
                        worker_id=worker_id,
                        artifact_store=artifact_store,
                    )
                )
                active.add(task)
                claimed = True
            if not claimed:
                await asyncio.sleep(_IDLE_SECONDS)
    finally:
        cleanup_supervisor.cancel()
        email_supervisor.cancel()
        for task in active:
            task.cancel()
        await asyncio.gather(
            cleanup_supervisor,
            email_supervisor,
            *active,
            return_exceptions=True,
        )
        await protection.release()
        logger.info("survey_supervisor_stopped")


async def _run_claimed_job(
    *,
    job: SurveyJob,
    worker_id: UUID,
    artifact_store: SurveyArtifactStore,
) -> None:
    try:
        await process_survey_job(
            job=job,
            worker_id=worker_id,
            artifact_store=artifact_store,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("survey_task_escaped", job_id=str(job.id))


__all__ = [
    "RCM_VERSION",
    "WORKFLOW_VERSION",
    "SurveyExecutionResult",
    "SurveyJobExecutor",
    "execute_survey",
    "process_survey_job",
    "serve_survey_worker",
]
