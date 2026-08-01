"""Concurrent Scholight Survey execution and durable artifact archiving."""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
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
from scholight.survey.artifacts import SurveyArtifactStore
from scholight.survey.cleanup_worker import serve_artifact_cleanup
from scholight.survey.contracts import SurveyLeaseLostError
from scholight.survey.process import (
    ProcessControl,
    classify_rcm_error,
    read_sanitized_tail,
    terminate_process_group,
    write_stdin,
)
from scholight.survey.progress import stage_for_component
from scholight.survey.runtime import survey_environment

logger = structlog.get_logger(__name__)

RCM_VERSION = "0.2.6"
WORKFLOW_VERSION = "scholight-survey-v1"
_IDLE_SECONDS = 1
_RECOVERY_SECONDS = 30
_EVENT_READ_BYTES = 64 * 1024
_EVENT_LINE_LIMIT = 1024 * 1024
_STAGE_RECORD_LIMIT = 512


@dataclass(frozen=True, slots=True)
class SurveyExecutionResult:
    outcome: Literal["succeeded", "failed"]
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime
    stage_timings: tuple[dict[str, object], ...] = ()


def _workflow_file() -> Path:
    return Path(__file__).parent / "workflow" / "rcm" / "survey_pipeline.rcm"


def _job_root(job_id: UUID) -> Path:
    return Path(settings.data_root) / "surveys" / str(job_id)


def _child_environment(*, user_id: int) -> dict[str, str]:
    return survey_environment(
        user_id=user_id,
        lifetime_seconds=settings.survey_job_timeout_seconds,
        include_image=True,
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
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            name = event.get("name")
            kind = event.get("kind")
            index = event.get("index")
            if not isinstance(name, str) or not isinstance(kind, str) or not isinstance(index, int):
                continue
            key = (name, kind, index)
            now = datetime.now(UTC)
            if event_type == "component_start":
                active[key] = (now, time.monotonic())
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
        if len(buffer) > _EVENT_LINE_LIMIT:
            buffer.clear()
            discard_until_newline = True
    return tuple(records)


def _timeout_message(seconds: int) -> str:
    hours = max(1, round(seconds / 3600))
    return f"Survey generation exceeded its {hours}-hour execution window."


async def execute_survey(
    job: SurveyJob,
    run_root: Path,
    *,
    control: ProcessControl | None = None,
) -> SurveyExecutionResult:
    """Run the fixed RCM workflow without retaining unbounded subprocess output."""
    started_at = datetime.now(UTC)
    process = await asyncio.create_subprocess_exec(
        "accelerate",
        "run",
        str(_workflow_file()),
        "--stream",
        "--purpose-stdin",
        "--run-dir",
        str(run_root),
        env=_child_environment(user_id=job.user_id),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    control = control or ProcessControl()
    await control.attach(process)
    stage_collector = asyncio.create_task(
        _collect_stage_timings(
            process.stdout,
            job_id=job.id,
            worker_id=job.lease_owner,
        )
    )
    stderr_task = asyncio.create_task(read_sanitized_tail(process.stderr))
    wait_task = asyncio.create_task(process.wait())
    lost_task = asyncio.create_task(control.lease_lost.wait())

    async def _complete_process() -> tuple[int, tuple[dict[str, object], ...]]:
        await write_stdin(process, job.approved_draft)
        return_code, stage_timings = await asyncio.gather(wait_task, stage_collector)
        return return_code, stage_timings

    lifecycle_task = asyncio.create_task(_complete_process())
    try:
        done, _pending = await asyncio.wait(
            {lifecycle_task, lost_task},
            timeout=settings.survey_job_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lost_task in done and control.lease_lost.is_set():
            await terminate_process_group(process)
            raise SurveyLeaseLostError("Survey execution lease is no longer owned")
        if not done:
            await terminate_process_group(process)
            stage_timings = await stage_collector
            return SurveyExecutionResult(
                outcome="failed",
                error_code="survey_timed_out",
                error_message=_timeout_message(settings.survey_job_timeout_seconds),
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stage_timings=stage_timings,
            )
        try:
            return_code, stage_timings = await lifecycle_task
        except Exception:
            await terminate_process_group(process)
            raise
        stderr_tail = await stderr_task
        if return_code != 0:
            error_code, error_message = classify_rcm_error(stderr_tail)
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
            )
        if not _valid_final_report(run_root):
            return SurveyExecutionResult(
                outcome="failed",
                error_code="survey_report_missing",
                error_message="Survey generation did not produce a final report.",
                started_at=started_at,
                finished_at=datetime.now(UTC),
                stage_timings=stage_timings,
            )
        return SurveyExecutionResult(
            outcome="succeeded",
            error_code=None,
            error_message=None,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            stage_timings=stage_timings,
        )
    except asyncio.CancelledError:
        await terminate_process_group(process)
        raise
    finally:
        lost_task.cancel()
        if process.returncode is None:
            await terminate_process_group(process)
        for task in (lifecycle_task, wait_task, stage_collector, stderr_task, lost_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            lifecycle_task,
            wait_task,
            stage_collector,
            stderr_task,
            lost_task,
            return_exceptions=True,
        )


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
            if state == "owned":
                last_owned = time.monotonic()
                continue
            if state == "lost" or time.monotonic() - last_owned >= settings.survey_lease_seconds:
                logger.warning("survey_heartbeat_lease_lost", job_id=str(job_id))
                await control.lose_lease()
                return


def _run_metadata(job: SurveyJob, result: SurveyExecutionResult | None) -> dict[str, object]:
    return {
        "schema_version": 1,
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


async def serve_survey_worker() -> None:
    """Supervise bounded concurrent Surveys with independent leases and process groups."""
    artifact_store = SurveyArtifactStore(
        bucket=settings.survey_s3_bucket,
        endpoint_url=settings.survey_s3_endpoint_url,
    )
    active: set[asyncio.Task[None]] = set()
    cleanup_supervisor = asyncio.create_task(serve_artifact_cleanup())
    last_recovery = 0.0
    logger.info(
        "survey_supervisor_started",
        rcm_version=RCM_VERSION,
        concurrency=settings.survey_job_concurrency,
        per_user_concurrency=settings.survey_job_per_user_concurrency,
        heartbeat_seconds=settings.survey_heartbeat_seconds,
        lease_seconds=settings.survey_lease_seconds,
    )
    try:
        while True:
            active = {task for task in active if not task.done()}
            if cleanup_supervisor.done():
                try:
                    cleanup_supervisor.result()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("survey_cleanup_supervisor_restarted")
                cleanup_supervisor = asyncio.create_task(serve_artifact_cleanup())
            now = time.monotonic()
            if now - last_recovery >= _RECOVERY_SECONDS:
                try:
                    await recover_expired_survey_jobs()
                except Exception:
                    logger.exception("survey_recovery_cycle_failed")
                last_recovery = now
            claimed = False
            while len(active) < settings.survey_job_concurrency:
                worker_id = uuid4()
                try:
                    job = await claim_survey_job(
                        worker_id=worker_id,
                        lease_seconds=settings.survey_lease_seconds,
                        per_user_concurrency=settings.survey_job_per_user_concurrency,
                    )
                except Exception:
                    logger.exception("survey_claim_cycle_failed")
                    break
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
        for task in active:
            task.cancel()
        await asyncio.gather(cleanup_supervisor, *active, return_exceptions=True)
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
