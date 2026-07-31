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
from scholight.survey.draft_worker import execute_draft


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
        error_code=None,
        error_message=None,
        lease_owner=uuid4(),
        lease_expires_at=now,
        heartbeat_at=now,
        created_at=now,
        started_at=now,
        finished_at=None,
    )


class _Process:
    def __init__(self, output: dict[str, object], return_code: int = 0) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(json.dumps(output).encode())
        self.stdout.feed_eof()
        self.return_code = return_code

    async def wait(self) -> int:
        return self.return_code

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

    with patch(
        "scholight.survey.draft_worker.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=process,
    ) as create_process:
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

    with patch(
        "scholight.survey.draft_worker.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=_Process({"message": "  "}),
    ):
        result = await execute_draft(draft=draft, context=context)

    assert result.error_code == "survey_draft_empty"
