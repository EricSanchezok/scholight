"""Draft RCM output and context assembly contracts."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from scholight.config import settings
from scholight.db.queries_survey_drafts import SurveyDraft, SurveyDraftContext
from scholight.survey.draft_worker import (
    _heartbeat,
    _run_claimed_draft,
    execute_draft,
    serve_survey_draft_worker,
)
from scholight.survey.process import ProcessControl


def _draft() -> SurveyDraft:
    now = datetime.now(UTC)
    return SurveyDraft(
        id=uuid4(),
        survey_id=uuid4(),
        user_id=42,
        revision=None,
        source="generated",
        user_message="Focus on evaluation methodology.",
        markdown=None,
        status="running",
        based_on_revision=1,
        client_request_id=uuid4(),
        request_hash="1" * 64,
        error_code=None,
        error_message=None,
        lease_owner=uuid4(),
        lease_expires_at=now,
        heartbeat_at=now,
        queued_at=now,
        last_claim_at=now,
        created_at=now,
        started_at=now,
        finished_at=None,
    )


class _Process:
    def __init__(self, output: dict[str, object], return_code: int = 0) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(json.dumps(output).encode())
        self.stdout.feed_eof()
        self.stderr = None
        self.stdin = None
        self.returncode: int | None = return_code

    async def wait(self) -> int:
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:  # pragma: no cover - timeout path uses a separate fake
        pass

    def kill(self) -> None:  # pragma: no cover - timeout path uses a separate fake
        pass


@pytest.mark.asyncio
async def test_draft_worker_persists_final_message_without_run_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = _draft()
    context = SurveyDraftContext(
        initial_request="Survey retrieval-augmented generation.",
        history=(("Initial request", "# Draft 1"),),
    )
    monkeypatch.setattr(settings, "survey_draft_timeout_seconds", 1800)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")
    message = "\n# Refined survey\n\nScope.\n"
    process = _Process({"message": message})

    with (
        patch(
            "scholight.survey.draft_worker.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ) as create_process,
        patch("scholight.survey.draft_worker.write_stdin", new_callable=AsyncMock),
    ):
        result = await execute_draft(draft=draft, context=context)

    assert result.markdown == message
    call = create_process.await_args
    assert call is not None
    command = call.args
    assert "--format" in command and "json" in command
    assert "--stream" not in command
    assert "--run-dir" not in command


@pytest.mark.asyncio
async def test_empty_draft_message_is_a_failed_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    draft = replace(_draft(), based_on_revision=None)
    context = SurveyDraftContext(initial_request="Topic", history=())
    monkeypatch.setattr(settings, "survey_draft_timeout_seconds", 1800)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")

    with (
        patch(
            "scholight.survey.draft_worker.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=_Process({"message": "  "}),
        ),
        patch("scholight.survey.draft_worker.write_stdin", new_callable=AsyncMock),
    ):
        result = await execute_draft(draft=draft, context=context)

    assert result.error_code == "survey_invalid_output"


@pytest.mark.asyncio
async def test_invalid_draft_output_logs_shape_without_model_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = replace(_draft(), based_on_revision=None)
    context = SurveyDraftContext(initial_request="Topic", history=())
    monkeypatch.setattr(settings, "survey_draft_timeout_seconds", 1800)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")

    with (
        patch(
            "scholight.survey.draft_worker.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=_Process({"unexpected": "private model response"}),
        ),
        patch("scholight.survey.draft_worker.write_stdin", new_callable=AsyncMock),
        patch("scholight.survey.draft_worker.logger.error") as log_error,
    ):
        result = await execute_draft(draft=draft, context=context)

    assert result.error_code == "survey_invalid_output"
    fields = log_error.call_args.kwargs
    assert fields["stdout_json_type"] == "dict"
    assert fields["stdout_keys"] == ["unexpected"]
    assert fields["output_bytes"] > 0
    assert "private model response" not in repr(fields)


@pytest.mark.asyncio
async def test_draft_timeout_covers_blocked_stdin_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    draft = _draft()
    context = SurveyDraftContext(initial_request="Topic", history=())
    process = _Process({"message": "unused"})
    process.returncode = None
    stopped = asyncio.Event()
    monkeypatch.setattr(settings, "survey_draft_timeout_seconds", 0.01)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")

    async def _blocked_stdin(candidate: object, purpose: str) -> None:
        del candidate, purpose
        await asyncio.Event().wait()

    async def _terminate(candidate: _Process) -> None:
        candidate.returncode = -15
        stopped.set()

    with (
        patch(
            "scholight.survey.draft_worker.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
        patch("scholight.survey.draft_worker.write_stdin", side_effect=_blocked_stdin),
        patch("scholight.survey.draft_worker.terminate_process_group", side_effect=_terminate),
    ):
        result = await asyncio.wait_for(
            execute_draft(draft=draft, context=context),
            timeout=1,
        )

    assert result.error_code == "survey_timed_out"
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_draft_heartbeat_database_failure_stops_work_after_lease_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = ProcessControl()
    monkeypatch.setattr(settings, "survey_heartbeat_seconds", 0.001)
    monkeypatch.setattr(settings, "survey_lease_seconds", 0)

    with (
        patch(
            "scholight.survey.draft_worker.heartbeat_survey_draft",
            new_callable=AsyncMock,
            side_effect=OSError("database unavailable"),
        ) as heartbeat,
    ):
        await asyncio.wait_for(
            _heartbeat(
                draft_id=uuid4(),
                worker_id=uuid4(),
                stop=asyncio.Event(),
                control=control,
            ),
            timeout=1,
        )

    heartbeat.assert_awaited_once()
    assert control.lease_lost.is_set()


@pytest.mark.asyncio
async def test_draft_supervisor_keeps_execution_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drafts = [_draft() for _ in range(5)]
    running = 0
    maximum_running = 0
    started = 0
    at_capacity = asyncio.Event()
    all_started = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(settings, "survey_draft_concurrency", 3)
    monkeypatch.setattr(settings, "survey_draft_per_user_concurrency", 2)

    async def _claim(**kwargs: object) -> SurveyDraft | None:
        del kwargs
        return drafts.pop(0) if drafts else None

    async def _run(draft: SurveyDraft, worker_id: object) -> None:
        nonlocal running, maximum_running, started
        del draft, worker_id
        running += 1
        started += 1
        maximum_running = max(maximum_running, running)
        if running == 3:
            at_capacity.set()
        if started == 5:
            all_started.set()
        try:
            await release.wait()
        finally:
            running -= 1

    with (
        patch("scholight.survey.draft_worker.claim_survey_draft", side_effect=_claim),
        patch("scholight.survey.draft_worker._run_claimed_draft", side_effect=_run),
        patch(
            "scholight.survey.draft_worker.recover_expired_survey_drafts",
            new_callable=AsyncMock,
        ),
        patch("scholight.survey.draft_worker._IDLE_SECONDS", 0.001),
    ):
        supervisor = asyncio.create_task(serve_survey_draft_worker())
        await asyncio.wait_for(at_capacity.wait(), timeout=1)
        assert running == 3
        release.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        supervisor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await supervisor

    assert maximum_running == 3


@pytest.mark.asyncio
async def test_draft_task_failure_does_not_escape_supervisor_boundary() -> None:
    draft = _draft()
    with patch(
        "scholight.survey.draft_worker.process_survey_draft",
        new_callable=AsyncMock,
        side_effect=RuntimeError("isolated failure"),
    ):
        await _run_claimed_draft(draft, uuid4())
