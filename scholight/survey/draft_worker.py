"""Concurrent Draft RCM supervisor with lease-owned subprocesses."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

import structlog

from scholight.config import settings
from scholight.db.queries_survey_drafts import (
    SurveyDraft,
    SurveyDraftContext,
    claim_survey_draft,
    complete_survey_draft,
    fail_survey_draft,
    get_survey_draft_context,
    heartbeat_survey_draft,
    recover_expired_survey_drafts,
)
from scholight.survey.contracts import (
    DRAFT_CONTEXT_MAX_BYTES,
    DRAFT_OUTPUT_MAX_BYTES,
    SurveyLeaseLostError,
    utf8_size,
)
from scholight.survey.process import (
    ProcessControl,
    ProcessOutputTooLargeError,
    classify_rcm_error,
    read_bounded,
    read_sanitized_tail,
    terminate_process_group,
    write_stdin,
)
from scholight.survey.runtime import survey_environment

logger = structlog.get_logger(__name__)

_IDLE_SECONDS = 1
_RECOVERY_SECONDS = 30


@dataclass(frozen=True, slots=True)
class DraftExecutionResult:
    markdown: str | None
    error_code: str | None
    error_message: str | None


def _workflow_file() -> str:
    return str(Path(__file__).parent / "workflow" / "rcm" / "draft.rcm")


def _purpose(*, draft: SurveyDraft, context: SurveyDraftContext) -> str:
    sections = ["# Initial request", context.initial_request, "", "# Successful Draft history"]
    if context.history:
        for revision, (message, markdown) in enumerate(context.history, start=1):
            sections.extend(
                [
                    "",
                    f"## Revision {revision}: user message",
                    message,
                    "",
                    f"## Revision {revision}: Draft",
                    markdown,
                ]
            )
    else:
        sections.append("No successful Draft exists yet.")
    sections.extend(["", "# Current user request", draft.user_message])
    purpose = "\n".join(sections)
    if utf8_size(purpose) > DRAFT_CONTEXT_MAX_BYTES:
        raise ValueError("Survey Draft context exceeds 8 MiB")
    return purpose


def _timeout_message(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return f"Draft generation exceeded its {minutes}-minute execution window."


async def execute_draft(
    *,
    draft: SurveyDraft,
    context: SurveyDraftContext,
    control: ProcessControl | None = None,
) -> DraftExecutionResult:
    control = control or ProcessControl()
    purpose = _purpose(draft=draft, context=context)
    process = await asyncio.create_subprocess_exec(
        "accelerate",
        "run",
        _workflow_file(),
        "--format",
        "json",
        "--purpose-stdin",
        env=survey_environment(
            user_id=draft.user_id,
            lifetime_seconds=settings.survey_draft_timeout_seconds,
            include_image=False,
        ),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    await control.attach(process)
    stderr_task = asyncio.create_task(read_sanitized_tail(process.stderr))
    output_task = asyncio.create_task(read_bounded(process.stdout, limit=DRAFT_OUTPUT_MAX_BYTES))
    wait_task = asyncio.create_task(process.wait())
    lost_task = asyncio.create_task(control.lease_lost.wait())

    async def _complete_process() -> tuple[int, bytes]:
        await write_stdin(process, purpose)
        return_code, output = await asyncio.gather(wait_task, output_task)
        return return_code, output

    lifecycle_task = asyncio.create_task(_complete_process())
    try:
        done, _pending = await asyncio.wait(
            {lifecycle_task, lost_task},
            timeout=settings.survey_draft_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lost_task in done and control.lease_lost.is_set():
            await terminate_process_group(process)
            raise SurveyLeaseLostError("Survey Draft lease is no longer owned")
        if not done:
            await terminate_process_group(process)
            return DraftExecutionResult(
                markdown=None,
                error_code="survey_timed_out",
                error_message=_timeout_message(settings.survey_draft_timeout_seconds),
            )
        try:
            return_code, output = await lifecycle_task
        except ProcessOutputTooLargeError:
            await terminate_process_group(process)
            return DraftExecutionResult(
                markdown=None,
                error_code="survey_draft_output_too_large",
                error_message="Draft generation returned more than 2 MiB of output.",
            )
        except Exception:
            await terminate_process_group(process)
            raise
        stderr_tail = await stderr_task
        if return_code != 0:
            error_code, error_message = classify_rcm_error(stderr_tail)
            logger.error(
                "survey_draft_rcm_failed",
                draft_id=str(draft.id),
                return_code=return_code,
                diagnostics=stderr_tail,
            )
            return DraftExecutionResult(None, error_code, error_message)
        try:
            payload = json.loads(output)
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, str) or not message.strip():
            stdout_keys = (
                sorted(str(key) for key in payload)[:16] if isinstance(payload, dict) else []
            )
            logger.error(
                "survey_draft_invalid_output",
                draft_id=str(draft.id),
                output_bytes=len(output),
                stdout_json_type=type(payload).__name__,
                stdout_keys=stdout_keys,
                diagnostics=stderr_tail,
            )
            return DraftExecutionResult(
                None,
                "survey_invalid_output",
                "Draft generation returned no usable Markdown.",
            )
        return DraftExecutionResult(message, None, None)
    except asyncio.CancelledError:
        await terminate_process_group(process)
        raise
    finally:
        lost_task.cancel()
        if process.returncode is None:
            await terminate_process_group(process)
        for task in (lifecycle_task, wait_task, output_task, stderr_task, lost_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(
            lifecycle_task,
            wait_task,
            output_task,
            stderr_task,
            lost_task,
            return_exceptions=True,
        )


async def _heartbeat(
    *,
    draft_id: UUID,
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
                state = await heartbeat_survey_draft(
                    draft_id=draft_id,
                    worker_id=worker_id,
                    lease_seconds=settings.survey_lease_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "survey_draft_heartbeat_failed",
                    draft_id=str(draft_id),
                    error_type=type(exc).__name__,
                )
                if time.monotonic() - last_owned < settings.survey_lease_seconds:
                    continue
                state = "lost"
            if state == "owned":
                last_owned = time.monotonic()
                continue
            if state == "lost" or time.monotonic() - last_owned >= settings.survey_lease_seconds:
                logger.warning("survey_draft_lease_lost", draft_id=str(draft_id))
                await control.lose_lease()
                return


async def process_survey_draft(*, draft: SurveyDraft, worker_id: UUID) -> None:
    stop = asyncio.Event()
    control = ProcessControl()
    heartbeat = asyncio.create_task(
        _heartbeat(draft_id=draft.id, worker_id=worker_id, stop=stop, control=control)
    )
    try:
        context = await get_survey_draft_context(survey_id=draft.survey_id)
        result = await execute_draft(draft=draft, context=context, control=control)
        if control.lease_lost.is_set():
            return
        if result.markdown is not None:
            await complete_survey_draft(
                draft_id=draft.id,
                worker_id=worker_id,
                markdown=result.markdown,
            )
        else:
            await fail_survey_draft(
                draft_id=draft.id,
                worker_id=worker_id,
                error_code=result.error_code or "survey_runtime_unavailable",
                error_message=result.error_message or "Draft generation did not complete.",
            )
    except SurveyLeaseLostError:
        logger.info("survey_draft_stopped_after_lease_loss", draft_id=str(draft.id))
    except asyncio.CancelledError:
        await control.lose_lease()
        raise
    except Exception as exc:
        logger.exception(
            "survey_draft_task_failed",
            draft_id=str(draft.id),
            error_type=type(exc).__name__,
        )
        if not control.lease_lost.is_set():
            try:
                await fail_survey_draft(
                    draft_id=draft.id,
                    worker_id=worker_id,
                    error_code="survey_runtime_unavailable",
                    error_message="Draft generation could not be completed.",
                )
            except Exception:
                logger.warning("survey_draft_failure_not_settled", draft_id=str(draft.id))
    finally:
        stop.set()
        await heartbeat


async def _run_claimed_draft(draft: SurveyDraft, worker_id: UUID) -> None:
    try:
        await process_survey_draft(draft=draft, worker_id=worker_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("survey_draft_task_escaped", draft_id=str(draft.id))


async def serve_survey_draft_worker() -> None:
    """Supervise bounded concurrent Drafts without sharing task failure state."""
    active: set[asyncio.Task[None]] = set()
    last_recovery = 0.0
    logger.info(
        "survey_draft_supervisor_started",
        global_concurrency=settings.survey_draft_global_concurrency,
        worker_concurrency=settings.survey_draft_worker_concurrency,
        per_user_concurrency=settings.survey_draft_per_user_concurrency,
        heartbeat_seconds=settings.survey_heartbeat_seconds,
        lease_seconds=settings.survey_lease_seconds,
    )
    try:
        while True:
            active = {task for task in active if not task.done()}
            now = time.monotonic()
            if now - last_recovery >= _RECOVERY_SECONDS:
                try:
                    await recover_expired_survey_drafts()
                except Exception:
                    logger.exception("survey_draft_recovery_cycle_failed")
                last_recovery = now
            claimed = False
            while len(active) < settings.survey_draft_worker_concurrency:
                worker_id = uuid4()
                try:
                    draft = await claim_survey_draft(
                        worker_id=worker_id,
                        lease_seconds=settings.survey_lease_seconds,
                        global_concurrency=settings.survey_draft_global_concurrency,
                        per_user_concurrency=settings.survey_draft_per_user_concurrency,
                    )
                except Exception:
                    logger.exception("survey_draft_claim_cycle_failed")
                    break
                if draft is None:
                    break
                task = asyncio.create_task(_run_claimed_draft(draft, worker_id))
                active.add(task)
                claimed = True
            if not claimed:
                await asyncio.sleep(_IDLE_SECONDS)
    finally:
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        logger.info("survey_draft_supervisor_stopped")


__all__ = [
    "DraftExecutionResult",
    "execute_draft",
    "process_survey_draft",
    "serve_survey_draft_worker",
]
