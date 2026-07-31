"""Single-process Scholight Survey execution and durable artifact archiving."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import jwt
import structlog

from scholight.config import settings
from scholight.db.queries_survey import (
    SurveyJob,
    claim_survey_job,
    defer_survey_archive,
    finish_survey_archive,
    heartbeat_survey_job,
    mark_survey_workspace_missing,
    recover_expired_survey_jobs,
    settle_survey_execution,
)
from scholight.survey.artifacts import SurveyArtifactStore

logger = structlog.get_logger(__name__)

RCM_VERSION = "0.2.1"
WORKFLOW_VERSION = "scholight-survey-v1"
_HEARTBEAT_SECONDS = 30
_LEASE_SECONDS = 300
_IDLE_SECONDS = 5
_EVENT_READ_BYTES = 64 * 1024
_EVENT_LINE_LIMIT = 1024 * 1024
_STAGE_RECORD_LIMIT = 512


@dataclass(frozen=True, slots=True)
class SurveyExecutionResult:
    outcome: str
    error_code: str | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime
    stage_timings: tuple[dict[str, object], ...] = ()


def _workflow_file() -> Path:
    return Path(__file__).parent / "workflow" / "rcm" / "survey_pipeline.rcm"


def _job_root(job_id: UUID) -> Path:
    return Path(settings.data_root) / "surveys" / str(job_id)


def _delegated_authorization(*, user_id: int) -> str:
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": "scholight-survey",
            "aud": "scholight-mcp",
            "sub": str(user_id),
            "scope": "search",
            "iat": int(now.timestamp()),
            "exp": int(
                (
                    now
                    + timedelta(seconds=settings.survey_job_timeout_seconds)
                    + timedelta(minutes=15)
                ).timestamp()
            ),
            "jti": str(uuid4()),
        },
        settings.survey_mcp_jwt_secret,
        algorithm="HS256",
    )
    return f"Bearer {token}"


def _child_environment(*, user_id: int) -> dict[str, str]:
    environment = {
        "HOME": os.environ.get("HOME", "/home/scholight"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "NO_PROXY": "api,localhost,127.0.0.1",
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
    }
    for name in ("SSL_CERT_DIR", "SSL_CERT_FILE", "TZ"):
        if value := os.environ.get(name):
            environment[name] = value
    environment["DEEPSEEK_API_KEY"] = settings.deepseek_api_key
    if settings.image_gen_api_key:
        environment["IMAGE_GEN_API_KEY"] = settings.image_gen_api_key
    environment["SCHOLIGHT_SURVEY_MCP_AUTHORIZATION"] = _delegated_authorization(user_id=user_id)
    return environment


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


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _collect_stage_timings(
    stream: asyncio.StreamReader | None,
) -> tuple[dict[str, object], ...]:
    """Drain RCM events while retaining only bounded, non-content stage metadata."""
    if not isinstance(stream, asyncio.StreamReader):
        return ()
    buffer = bytearray()
    discard_until_newline = False
    active: dict[tuple[str, str, int], tuple[datetime, float]] = {}
    records: list[dict[str, object]] = []

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


async def execute_survey(job: SurveyJob, run_root: Path) -> SurveyExecutionResult:
    """Run the fixed RCM workflow without retaining unbounded subprocess output."""
    started_at = datetime.now(UTC)
    process = await asyncio.create_subprocess_exec(
        "accelerate",
        "run",
        str(_workflow_file()),
        "--stream",
        "--purpose",
        job.topic,
        "--run-dir",
        str(run_root),
        env=_child_environment(user_id=job.user_id),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stage_collector = asyncio.create_task(_collect_stage_timings(process.stdout))
    try:
        return_code = await asyncio.wait_for(
            process.wait(),
            timeout=settings.survey_job_timeout_seconds,
        )
    except TimeoutError:
        await _stop_process(process)
        stage_timings = await stage_collector
        return SurveyExecutionResult(
            outcome="failed",
            error_code="survey_timed_out",
            error_message="Survey generation exceeded the 24-hour execution window.",
            started_at=started_at,
            finished_at=datetime.now(UTC),
            stage_timings=stage_timings,
        )
    stage_timings = await stage_collector
    if return_code != 0:
        return SurveyExecutionResult(
            outcome="failed",
            error_code="survey_execution_failed",
            error_message="Survey generation did not complete successfully.",
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


async def _heartbeat(*, job_id: UUID, worker_id: UUID, stop: asyncio.Event) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_SECONDS)
            return
        except TimeoutError:
            try:
                owned = await heartbeat_survey_job(
                    job_id=job_id,
                    worker_id=worker_id,
                    lease_seconds=_LEASE_SECONDS,
                )
                if not owned:
                    logger.warning("survey_heartbeat_lease_lost", job_id=str(job_id))
                    return
            except Exception as exc:
                logger.error(
                    "survey_heartbeat_failed",
                    job_id=str(job_id),
                    error_type=type(exc).__name__,
                )


def _run_metadata(job: SurveyJob, result: SurveyExecutionResult | None) -> dict[str, object]:
    return {
        "schema_version": 1,
        "job_id": str(job.id),
        "user_id": job.user_id,
        "topic": job.topic,
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
            storage_prefix=archive.storage_prefix,
            manifest_key=archive.manifest_key,
        )
    except Exception as exc:
        delay_seconds = min(3600, 30 * (2 ** min(job.archive_attempts, 7)))
        await defer_survey_archive(
            job_id=job.id,
            worker_id=worker_id,
            retry_after=timedelta(seconds=delay_seconds),
            error_code="survey_archive_failed",
            error_message="Survey artifacts could not be archived yet.",
        )
        logger.error(
            "survey_archive_failed",
            job_id=str(job.id),
            error_type=type(exc).__name__,
        )
        return
    shutil.rmtree(_job_root(job.id))
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
    heartbeat = asyncio.create_task(_heartbeat(job_id=job.id, worker_id=worker_id, stop=stop))
    try:
        if job.status == "running":
            run_root.mkdir(parents=True, exist_ok=True)
            try:
                result = await execute_survey(job, run_root)
            except Exception as exc:
                now = datetime.now(UTC)
                result = SurveyExecutionResult(
                    outcome="failed",
                    error_code="survey_execution_failed",
                    error_message="Survey generation did not complete successfully.",
                    started_at=job.started_at or now,
                    finished_at=now,
                )
                logger.error(
                    "survey_execution_failed",
                    job_id=str(job.id),
                    error_type=type(exc).__name__,
                )
            job = await settle_survey_execution(
                job_id=job.id,
                worker_id=worker_id,
                outcome=result.outcome,  # type: ignore[arg-type]
                error_code=result.error_code,
                error_message=result.error_message,
            )
        await _archive(
            job=job,
            worker_id=worker_id,
            run_root=run_root,
            artifact_store=artifact_store,
            result=result,
        )
    finally:
        stop.set()
        await heartbeat


async def serve_survey_worker() -> None:
    """Continuously run one Survey at a time and prioritize archive recovery."""
    worker_id = uuid4()
    artifact_store = SurveyArtifactStore(bucket=settings.survey_s3_bucket)
    logger.info("survey_worker_started", worker_id=str(worker_id), rcm_version=RCM_VERSION)
    while True:
        await recover_expired_survey_jobs()
        job = await claim_survey_job(
            worker_id=worker_id,
            lease_seconds=_LEASE_SECONDS,
        )
        if job is None:
            await asyncio.sleep(_IDLE_SECONDS)
            continue
        await process_survey_job(
            job=job,
            worker_id=worker_id,
            artifact_store=artifact_store,
        )


__all__ = [
    "RCM_VERSION",
    "WORKFLOW_VERSION",
    "SurveyExecutionResult",
    "execute_survey",
    "process_survey_job",
    "serve_survey_worker",
]
