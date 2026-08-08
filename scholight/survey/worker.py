"""Concurrent Scholight Survey execution and durable artifact archiving."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import stat
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import structlog

from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.queries_survey import (
    SurveyJob,
    claim_survey_job,
    defer_survey_archive,
    finish_survey_archive,
    heartbeat_survey_job,
    mark_survey_workspace_missing,
    recover_expired_survey_jobs,
    settle_survey_execution,
    update_survey_job_progress,
)
from scholight.db.queries_survey_capacity import get_survey_capacity_snapshot
from scholight.logging.emf import emit_emf
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
from scholight.survey.finalizer import SurveyFinalizationError, finalize_survey
from scholight.survey.metrics import is_provider_throttled
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
from scholight.survey.runtime import survey_environment
from scholight.survey.workflow_resources import (
    WorkflowResourceError,
    prepare_workflow_workspace,
    stage_workflow_schema,
)
from scholight.survey.workflow_runtime import workflow_file

logger = structlog.get_logger(__name__)

RCM_VERSION = "0.2.12"
WORKFLOW_VERSION = "scholight-survey-v1"
_IDLE_SECONDS = 1
_RECOVERY_SECONDS = 30
_EVENT_READ_BYTES = 64 * 1024
_EVENT_LINE_LIMIT = 1024 * 1024
_STAGE_RECORD_LIMIT = 512
_ARTIFACT_OBSERVE_SECONDS = 5
_ACTIVITY_METRIC_SECONDS = 60
_MODEL_TIMEOUT = re.compile(r"(?:timed out after|timeout(?: after)?)\s+(\d+)\s*s", re.I)


def _classify_model_hitch(preview: object) -> dict[str, object] | None:
    """Classify an RCM hitch in memory without persisting its model-provided text."""
    if not isinstance(preview, str):
        return None
    normalized = preview.casefold()
    timeout_match = _MODEL_TIMEOUT.search(preview)
    if timeout_match is not None:
        return {
            "error_code": "model_timeout",
            "timeout_seconds": int(timeout_match.group(1)),
        }
    status_match = re.search(r"(?:status|http)\D{0,8}([45]\d\d)", normalized)
    http_status = int(status_match.group(1)) if status_match is not None else None
    if http_status == 429 or "rate limit" in normalized:
        result: dict[str, object] = {"error_code": "model_rate_limited"}
    elif http_status in {401, 403} or any(
        marker in normalized for marker in ("unauthorized", "authentication", "invalid api key")
    ):
        result = {"error_code": "model_authentication_failed"}
    else:
        result = {"error_code": "model_completion_failed"}
    if http_status is not None:
        result["http_status"] = http_status
    return result


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
    return (
        stat.S_ISREG(report_stat.st_mode)
        and not report.is_symlink()
        and resolved_report.parent == resolved_root
        and report_stat.st_size > 0
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
    records: list[dict[str, object]] = []
    last_progress_stage: str | None = None

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
                diagnostics.model_event(status="finished", **completion_fields)
                continue
            if (
                diagnostics is not None
                and event_type in {"appended", "inserted", "replaced"}
                and event.get("kind") == "hitch"
                and event.get("role") in {"assistant", "system"}
            ):
                classification = _classify_model_hitch(event.get("preview"))
                if classification is not None:
                    diagnostics.model_event(status="failed", **classification)
                    continue
            if tool_status is not None and diagnostics is not None:
                tool_name = event.get("tool") or event.get("name")
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
                diagnostics.tool_event(
                    tool=str(tool_name or "unknown"),
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
    emit_emf(
        service="survey-worker",
        metrics={
            "SurveyContractAnomaly": (contract_errors, "Count"),
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
            error_code, error_message = classify_rcm_error(stderr_tail)
            diagnostic_summary = _finish_diagnostics(
                diagnostics,
                outcome="failed",
                return_code=return_code,
                termination_reason="nonzero_exit",
                audit_contract=True,
            )
            logger.error(
                "survey_rcm_failed",
                job_id=str(job.id),
                return_code=return_code,
                diagnostics=stderr_tail,
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
                stderr_tail=stderr_tail or None,
                diagnostics=diagnostic_summary,
            )
        try:
            if job.lease_owner is not None:
                await update_survey_job_progress(
                    job_id=job.id,
                    worker_id=job.lease_owner,
                    stage="finalizing",
                )
            finalized = finalize_survey(run_root)
            diagnostics.component_finished("survey_finalizer", status="completed")
            logger.info(
                "survey_report_finalized",
                job_id=str(job.id),
                section_count=finalized.section_count,
                reference_count=finalized.reference_count,
                unverified_reference_count=finalized.unverified_reference_count,
            )
        except SurveyFinalizationError as exc:
            logger.error(
                "survey_report_finalization_failed",
                job_id=str(job.id),
                error_type=type(exc).__name__,
                reason=str(exc),
            )
        if not _valid_final_report(run_root):
            diagnostic_summary = _finish_diagnostics(
                diagnostics,
                outcome="failed",
                return_code=return_code,
                termination_reason="report_missing",
                audit_contract=True,
            )
            logger.error(
                "survey_rcm_failed",
                job_id=str(job.id),
                return_code=return_code,
                diagnostics=stderr_tail,
                first_anomaly=diagnostic_summary.get("first_anomaly"),
            )
            return SurveyExecutionResult(
                outcome="failed",
                error_code="survey_report_missing",
                error_message="Survey generation did not produce a final report.",
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stage_timings=stage_timings,
                return_code=return_code,
                termination_reason="report_missing",
                stderr_tail=stderr_tail or None,
                diagnostics=diagnostic_summary,
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
            diagnostic_summary = _finish_diagnostics(
                diagnostics,
                outcome="failed",
                return_code=return_code,
                termination_reason="contract_violation",
                audit_contract=False,
            )
            logger.error(
                "survey_rcm_failed",
                job_id=str(job.id),
                return_code=return_code,
                diagnostics=stderr_tail,
                first_anomaly=diagnostic_summary.get("first_anomaly"),
                contract_error_count=len(contract_errors),
            )
            return SurveyExecutionResult(
                outcome="failed",
                error_code="survey_contract_violation",
                error_message="Survey generation produced incomplete required artifacts.",
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stage_timings=stage_timings,
                return_code=return_code,
                termination_reason="contract_violation",
                stderr_tail=stderr_tail or None,
                diagnostics=diagnostic_summary,
            )
        diagnostic_summary = _finish_diagnostics(
            diagnostics,
            outcome="succeeded",
            return_code=return_code,
            termination_reason="completed",
            audit_contract=False,
        )
        return SurveyExecutionResult(
            outcome="succeeded",
            error_code=None,
            error_message=None,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            stage_timings=stage_timings,
            return_code=return_code,
            termination_reason="completed",
            stderr_tail=(
                stderr_tail if stderr_tail and diagnostic_summary.get("anomaly_count", 0) else None
            ),
            diagnostics=diagnostic_summary,
        )
    except asyncio.CancelledError:
        await terminate_process_group(process)
        raise
    finally:
        artifact_stop.set()
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
            return_exceptions=True,
        )


async def execute_survey(
    job: SurveyJob,
    run_root: Path,
    *,
    control: ProcessControl | None = None,
) -> SurveyExecutionResult:
    """Run Survey with bounded provider retries and one artifact repair pass."""
    control = control or ProcessControl()
    provider_attempt = 1
    artifact_repair_available = True
    artifact_repair_failure: SurveyExecutionResult | None = None
    while provider_attempt <= settings.survey_provider_max_attempts:
        result = await _execute_survey_once(job, run_root, control=control)
        if artifact_repair_failure is not None and result.outcome == "failed":
            result = replace(
                result,
                started_at=artifact_repair_failure.started_at,
                stderr_tail=result.stderr_tail or artifact_repair_failure.stderr_tail,
                diagnostics=result.diagnostics or artifact_repair_failure.diagnostics,
            )
        if (
            artifact_repair_available
            and result.outcome == "failed"
            and result.error_code == "survey_report_missing"
            and result.return_code == 0
        ):
            artifact_repair_available = False
            artifact_repair_failure = result
            logger.warning(
                "survey_artifact_repair_scheduled",
                job_id=str(job.id),
                provider_attempt=provider_attempt,
            )
            emit_emf(
                service="survey-full-worker",
                metrics={"SurveyArtifactRepair": (1, "Count")},
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
                    termination_reason="cancelled_before_artifact_repair",
                )
            continue
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
        artifact_repair_available = True
        artifact_repair_failure = None
    raise AssertionError("unreachable")


async def _heartbeat(
    *,
    job_id: UUID,
    worker_id: UUID,
    stop: asyncio.Event,
    control: ProcessControl,
) -> None:
    last_owned = time.monotonic()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.survey_heartbeat_seconds)
            return
        except TimeoutError:
            started_at = time.perf_counter()
            try:
                state = await heartbeat_survey_job(
                    job_id=job_id,
                    worker_id=worker_id,
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
) -> None:
    """Execute a pending claim or resume archiving without rerunning RCM."""
    run_root = _job_root(job.id) / "run"
    result: SurveyExecutionResult | None = None
    stop = asyncio.Event()
    control = ProcessControl()
    heartbeat = asyncio.create_task(
        _heartbeat(job_id=job.id, worker_id=worker_id, stop=stop, control=control)
    )
    try:
        if job.status == "running":
            run_root.mkdir(parents=True, exist_ok=True)
            try:
                result = await execute_survey(job, run_root, control=control)
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
    "execute_survey",
    "process_survey_job",
    "serve_survey_worker",
]
