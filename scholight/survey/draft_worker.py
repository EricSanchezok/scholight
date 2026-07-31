"""Single-node Draft RCM execution with PostgreSQL as the only result store."""

from __future__ import annotations

import asyncio
import json
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
from scholight.survey.runtime import survey_environment

logger = structlog.get_logger(__name__)

_HEARTBEAT_SECONDS = 30
_LEASE_SECONDS = 300
_IDLE_SECONDS = 2


@dataclass(frozen=True, slots=True)
class DraftExecutionResult:
    markdown: str | None
    error_code: str | None
    error_message: str | None


def _workflow_file() -> str:
    return str(Path(__file__).parent / "workflow" / "rcm" / "draft.rcm")


def _purpose(*, draft: SurveyDraft, context: SurveyDraftContext) -> str:
    sections = [
        "# Initial request",
        context.initial_request,
        "",
        "# Successful Draft history",
    ]
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
    return "\n".join(sections)


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        process.kill()
        await process.wait()


async def execute_draft(*, draft: SurveyDraft, context: SurveyDraftContext) -> DraftExecutionResult:
    process = await asyncio.create_subprocess_exec(
        "accelerate",
        "run",
        _workflow_file(),
        "--format",
        "json",
        "--purpose",
        _purpose(draft=draft, context=context),
        env=survey_environment(
            user_id=draft.user_id,
            lifetime_seconds=settings.survey_draft_timeout_seconds,
            include_image=False,
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    if not isinstance(process.stdout, asyncio.StreamReader):
        await _stop_process(process)
        return DraftExecutionResult(
            markdown=None,
            error_code="survey_draft_output_unavailable",
            error_message="Draft generation returned no readable result.",
        )
    output_task = asyncio.create_task(process.stdout.read())
    try:
        return_code = await asyncio.wait_for(
            process.wait(), timeout=settings.survey_draft_timeout_seconds
        )
    except TimeoutError:
        await _stop_process(process)
        await output_task
        return DraftExecutionResult(
            markdown=None,
            error_code="survey_draft_timed_out",
            error_message="Draft generation exceeded the 30-minute execution window.",
        )
    output = await output_task
    if return_code != 0:
        return DraftExecutionResult(
            markdown=None,
            error_code="survey_draft_generation_failed",
            error_message="Draft generation did not complete successfully.",
        )
    try:
        payload = json.loads(output)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, str) or not message.strip():
        return DraftExecutionResult(
            markdown=None,
            error_code="survey_draft_empty",
            error_message="Draft generation returned an empty result.",
        )
    return DraftExecutionResult(markdown=message, error_code=None, error_message=None)


async def _heartbeat(*, draft_id: UUID, worker_id: UUID, stop: asyncio.Event) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_SECONDS)
            return
        except TimeoutError:
            try:
                if not await heartbeat_survey_draft(
                    draft_id=draft_id,
                    worker_id=worker_id,
                    lease_seconds=_LEASE_SECONDS,
                ):
                    logger.warning("survey_draft_lease_lost", draft_id=str(draft_id))
                    return
            except Exception as exc:
                logger.error(
                    "survey_draft_heartbeat_failed",
                    draft_id=str(draft_id),
                    error_type=type(exc).__name__,
                )


async def process_survey_draft(*, draft: SurveyDraft, worker_id: UUID) -> None:
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(draft_id=draft.id, worker_id=worker_id, stop=stop))
    try:
        context = await get_survey_draft_context(survey_id=draft.survey_id)
        try:
            result = await execute_draft(draft=draft, context=context)
        except Exception as exc:
            logger.error(
                "survey_draft_execution_failed",
                draft_id=str(draft.id),
                error_type=type(exc).__name__,
            )
            result = DraftExecutionResult(
                markdown=None,
                error_code="survey_draft_generation_failed",
                error_message="Draft generation did not complete successfully.",
            )
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
                error_code=result.error_code or "survey_draft_generation_failed",
                error_message=result.error_message or "Draft generation failed.",
            )
    finally:
        stop.set()
        await heartbeat


async def serve_survey_draft_worker() -> None:
    worker_id = uuid4()
    logger.info("survey_draft_worker_started", worker_id=str(worker_id))
    while True:
        await recover_expired_survey_drafts()
        draft = await claim_survey_draft(worker_id=worker_id, lease_seconds=_LEASE_SECONDS)
        if draft is None:
            await asyncio.sleep(_IDLE_SECONDS)
            continue
        await process_survey_draft(draft=draft, worker_id=worker_id)


__all__ = [
    "DraftExecutionResult",
    "execute_draft",
    "process_survey_draft",
    "serve_survey_draft_worker",
]
