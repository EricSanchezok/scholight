"""Survey worker execution, timeout, and archive-recovery tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from scholight.config import settings
from scholight.db.queries_survey import SurveyJob
from scholight.survey.artifacts import SurveyArchive
from scholight.survey.worker import (
    RCM_VERSION,
    SurveyExecutionResult,
    _child_environment,
    _collect_stage_timings,
    execute_survey,
    process_survey_job,
)


def test_worker_expects_pinned_rcm_release() -> None:
    assert RCM_VERSION == "0.2.4"


def _job(
    *,
    job_id: UUID,
    worker_id: UUID,
    status: str,
    outcome: str | None = None,
) -> SurveyJob:
    now = datetime.now(UTC)
    return SurveyJob(
        id=job_id,
        user_id=42,
        topic="retrieval augmented generation",
        status=status,  # type: ignore[arg-type]
        terminal_outcome=outcome,  # type: ignore[arg-type]
        quota_date=date(2026, 7, 31),
        storage_prefix=None,
        manifest_key=None,
        error_code=None,
        error_message=None,
        lease_owner=worker_id,
        lease_expires_at=now,
        heartbeat_at=now,
        archive_attempts=0,
        next_archive_at=None,
        created_at=now,
        started_at=now,
        finished_at=None,
    )


class _TimeoutProcess:
    stdout = None
    returncode = None

    def __init__(self) -> None:
        self.terminated = False

    async def wait(self) -> int:
        if not self.terminated:
            await asyncio.sleep(60)
        return -15

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


@pytest.mark.asyncio
async def test_execution_timeout_terminates_process_and_preserves_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    process = _TimeoutProcess()
    monkeypatch.setattr(settings, "survey_job_timeout_seconds", 0.01)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")
    monkeypatch.setattr(settings, "image_gen_api_key", "image")
    with patch(
        "scholight.survey.worker.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=process,
    ):
        result = await execute_survey(
            _job(job_id=job_id, worker_id=worker_id, status="running"),
            tmp_path,
        )

    assert result.error_code == "survey_timed_out"
    assert process.terminated is True
    assert tmp_path.exists()


@pytest.mark.asyncio
async def test_archiving_recovery_never_reruns_expensive_workflow(tmp_path: Path) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    job_root = tmp_path / str(job_id)
    run_root = job_root / "run"
    run_root.mkdir(parents=True)
    (run_root / "08_survey.md").write_text("# Survey", encoding="utf-8")
    artifact_store = AsyncMock()
    archive_result = SurveyArchive(
        storage_prefix=f"surveys/v1/42/{job_id}",
        manifest_key=f"surveys/v1/42/{job_id}/manifest.json",
        manifest={"files": []},
    )
    artifact_store.archive_run.return_value = archive_result
    with (
        patch("scholight.survey.worker._job_root", return_value=job_root),
        patch("scholight.survey.worker.execute_survey", new_callable=AsyncMock) as execute,
        patch("scholight.survey.worker._heartbeat", new_callable=AsyncMock) as heartbeat,
        patch(
            "scholight.survey.worker.finish_survey_archive",
            new_callable=AsyncMock,
        ) as finish,
    ):
        await process_survey_job(
            job=_job(
                job_id=job_id,
                worker_id=worker_id,
                status="archiving",
                outcome="succeeded",
            ),
            worker_id=worker_id,
            artifact_store=artifact_store,
        )

    execute.assert_not_awaited()
    heartbeat.assert_awaited_once()
    finish.assert_awaited_once()
    assert not job_root.exists()


@pytest.mark.asyncio
async def test_success_requires_nonempty_regular_final_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    (tmp_path / "08_survey.md").write_bytes(b"x" * (11 * 1024 * 1024))
    monkeypatch.setattr(settings, "survey_job_timeout_seconds", 60)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")
    monkeypatch.setattr(settings, "image_gen_api_key", "image")
    process = AsyncMock()
    process.wait.return_value = 0
    with patch(
        "scholight.survey.worker.asyncio.create_subprocess_exec",
        new_callable=AsyncMock,
        return_value=process,
    ):
        result = await execute_survey(
            _job(job_id=job_id, worker_id=worker_id, status="running"),
            tmp_path,
        )

    assert result.outcome == "succeeded"


@pytest.mark.asyncio
async def test_running_execution_settles_before_artifact_upload(tmp_path: Path) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    job_root = tmp_path / str(job_id)
    run_root = job_root / "run"
    run_root.mkdir(parents=True)
    result = SurveyExecutionResult(
        outcome="failed",
        error_code="survey_execution_failed",
        error_message="Survey generation did not complete successfully.",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
    )
    archiving = _job(
        job_id=job_id,
        worker_id=worker_id,
        status="archiving",
        outcome="failed",
    )
    artifact_store = AsyncMock()
    archive_result = SurveyArchive(
        storage_prefix=f"surveys/v1/42/{job_id}",
        manifest_key=f"surveys/v1/42/{job_id}/manifest.json",
        manifest={"files": []},
    )
    artifact_store.archive_run.return_value = archive_result
    order: list[str] = []

    async def _settle(**kwargs: object) -> SurveyJob:
        del kwargs
        order.append("settle")
        return archiving

    async def _archive(**kwargs: object) -> SurveyArchive:
        del kwargs
        order.append("archive")
        return archive_result

    artifact_store.archive_run.side_effect = _archive
    with (
        patch("scholight.survey.worker._job_root", return_value=job_root),
        patch(
            "scholight.survey.worker.execute_survey",
            new_callable=AsyncMock,
            return_value=result,
        ),
        patch("scholight.survey.worker._heartbeat", new_callable=AsyncMock),
        patch("scholight.survey.worker.settle_survey_execution", side_effect=_settle),
        patch("scholight.survey.worker.finish_survey_archive", new_callable=AsyncMock),
    ):
        await process_survey_job(
            job=_job(job_id=job_id, worker_id=worker_id, status="running"),
            worker_id=worker_id,
            artifact_store=artifact_store,
        )

    assert order == ["settle", "archive"]


@pytest.mark.asyncio
async def test_process_start_failure_is_settled_and_archived_without_waiting_for_lease(
    tmp_path: Path,
) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    job_root = tmp_path / str(job_id)
    archiving = _job(
        job_id=job_id,
        worker_id=worker_id,
        status="archiving",
        outcome="failed",
    )
    artifact_store = AsyncMock()
    artifact_store.archive_run.return_value = SurveyArchive(
        storage_prefix=f"surveys/v1/42/{job_id}",
        manifest_key=f"surveys/v1/42/{job_id}/manifest.json",
        manifest={"files": []},
    )

    with (
        patch("scholight.survey.worker._job_root", return_value=job_root),
        patch(
            "scholight.survey.worker.execute_survey",
            new_callable=AsyncMock,
            side_effect=OSError("accelerate executable unavailable"),
        ),
        patch("scholight.survey.worker._heartbeat", new_callable=AsyncMock),
        patch(
            "scholight.survey.worker.settle_survey_execution",
            new_callable=AsyncMock,
            return_value=archiving,
        ) as settle,
        patch("scholight.survey.worker.finish_survey_archive", new_callable=AsyncMock),
    ):
        await process_survey_job(
            job=_job(job_id=job_id, worker_id=worker_id, status="running"),
            worker_id=worker_id,
            artifact_store=artifact_store,
        )

    settled = settle.await_args
    assert settled is not None
    assert settled.kwargs["outcome"] == "failed"
    assert settled.kwargs["error_code"] == "survey_execution_failed"
    artifact_store.archive_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_stage_collector_keeps_only_safe_bounded_metadata() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(
        b'{"type":"component_start","name":"discovery","kind":"accelerator","index":1}\n'
        b'{"type":"appended","preview":"sensitive model content"}\n'
        b'{"type":"component_done","name":"discovery","kind":"accelerator","index":1}\n'
    )
    stream.feed_eof()

    records = await _collect_stage_timings(stream)

    assert len(records) == 1
    assert records[0]["name"] == "discovery"
    assert records[0]["status"] == "completed"
    assert "preview" not in records[0]


def test_child_environment_exposes_only_required_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHOLIGHT_PG_PASSWORD", "must-not-reach-rcm")
    monkeypatch.setenv("SCHOLIGHT_ZILLIZ_TOKEN", "must-not-reach-rcm")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-rcm")
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")
    monkeypatch.setattr(settings, "image_gen_api_key", "image")

    environment = _child_environment(user_id=42)

    assert environment["DEEPSEEK_API_KEY"] == "deepseek"
    assert environment["IMAGE_GEN_API_KEY"] == "image"
    assert environment["SCHOLIGHT_SURVEY_MCP_AUTHORIZATION"].startswith("Bearer ")
    assert "SCHOLIGHT_PG_PASSWORD" not in environment
    assert "SCHOLIGHT_ZILLIZ_TOKEN" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
