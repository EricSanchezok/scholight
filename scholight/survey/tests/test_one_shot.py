"""Standalone Survey task entry point contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import ANY, AsyncMock
from uuid import uuid4

import pytest

from scholight.db.queries_survey import SurveyJob
from scholight.db.queries_survey_attempts import SurveyCheckpointPointer
from scholight.survey.one_shot import (
    _record_result_provider_diagnostics,
    run_exact_draft,
    run_exact_job,
)
from scholight.survey.worker import SurveyExecutionResult


def _job(attempt_id: object) -> SurveyJob:
    now = datetime.now(UTC)
    return SurveyJob(
        id=uuid4(),
        survey_id=uuid4(),
        user_id=42,
        approved_draft_id=uuid4(),
        approved_draft="# Plan",
        approved_draft_revision=1,
        client_request_id=uuid4(),
        request_hash="a" * 64,
        status="running",
        terminal_outcome=None,
        storage_prefix=None,
        storage_bucket=None,
        manifest_key=None,
        error_code=None,
        error_message=None,
        lease_owner=attempt_id,  # type: ignore[arg-type]
        lease_expires_at=now,
        heartbeat_at=now,
        progress_stage="planning",
        progress_updated_at=now,
        archive_attempts=0,
        next_archive_at=None,
        queued_at=now,
        last_claim_at=now,
        created_at=now,
        started_at=now,
        finished_at=None,
    )


@pytest.mark.asyncio
async def test_duplicate_exact_draft_exits_without_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = AsyncMock(return_value=None)
    process = AsyncMock()
    monkeypatch.setattr("scholight.survey.one_shot.claim_exact_survey_draft", claim)
    monkeypatch.setattr("scholight.survey.one_shot.process_survey_draft", process)

    claimed = await run_exact_draft(draft_id=uuid4(), attempt_id=uuid4())

    assert not claimed
    process.assert_not_awaited()


@pytest.mark.asyncio
async def test_exact_job_restores_checkpoint_before_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_id = uuid4()
    job = _job(attempt_id)
    pointer = SurveyCheckpointPointer(
        sequence=3,
        stage="paper_card:one",
        manifest_key="checkpoint.json",
        manifest_sha256="a" * 64,
        workflow_version="workflow-v1",
        executor_version="executor-v1",
        execution_deadline_at=datetime.now(UTC),
    )
    restore = AsyncMock()
    find_successor = AsyncMock(return_value=None)
    process = AsyncMock()
    checkpoint_store = type(
        "CheckpointStore",
        (),
        {"restore": restore, "find_successor": find_successor},
    )()
    artifact_store = object()
    monkeypatch.setattr(
        "scholight.survey.one_shot.claim_exact_survey_job", AsyncMock(return_value=job)
    )
    monkeypatch.setattr(
        "scholight.survey.one_shot.get_claimed_job_checkpoint",
        AsyncMock(return_value=pointer),
    )
    monkeypatch.setattr(
        "scholight.survey.one_shot.SurveyCheckpointStore",
        lambda **_kwargs: checkpoint_store,
    )
    monkeypatch.setattr(
        "scholight.survey.one_shot.SurveyArtifactStore",
        lambda **_kwargs: artifact_store,
    )
    monkeypatch.setattr("scholight.survey.one_shot.process_survey_job", process)
    monkeypatch.setattr("scholight.survey.one_shot.settings.data_root", tmp_path)
    monkeypatch.setattr("scholight.survey.one_shot.settings.survey_s3_bucket", "bucket")

    claimed = await run_exact_job(job_id=job.id, attempt_id=attempt_id)

    assert claimed
    restore.assert_awaited_once()
    process.assert_awaited_once_with(
        job=job,
        worker_id=attempt_id,
        artifact_store=artifact_store,
        attempt_id=attempt_id,
        execute_job=ANY,
    )


@pytest.mark.asyncio
async def test_result_provider_diagnostics_are_mapped_to_attempt_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_id = uuid4()
    now = datetime.now(UTC)
    record = AsyncMock()
    monkeypatch.setattr(
        "scholight.survey.one_shot.record_compute_attempt_diagnostics",
        record,
    )
    result = SurveyExecutionResult(
        outcome="succeeded",
        error_code="survey_quality_degraded",
        error_message=None,
        started_at=now,
        finished_at=now,
        diagnostics={
            "provider_failures": [
                {
                    "unit": "reference_seed:2401.00001",
                    "http_status": 400,
                    "provider_code": "context_length_exceeded",
                    "request_class": "request_size",
                    "serialized_request_bytes": 900_000,
                    "prompt": "must not be retained",
                }
            ]
        },
        chargeable=False,
    )

    await _record_result_provider_diagnostics(attempt_id=attempt_id, result=result)

    record.assert_awaited_once_with(
        attempt_id=attempt_id,
        failure_class="provider_request_size",
        failure_details={
            "provider_status": 400,
            "provider_code": "context_length_exceeded",
            "provider_request_class": "request_size",
            "request_bytes": 900_000,
        },
    )
