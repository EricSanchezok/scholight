"""Survey worker execution, timeout, and archive-recovery tests."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import jwt
import pytest

from scholight.config import settings
from scholight.db.queries_survey import SurveyJob
from scholight.survey.artifacts import SurveyArchive
from scholight.survey.diagnostics import ARTIFACT_CONTRACTS, SurveyDiagnostics
from scholight.survey.process import ProcessControl
from scholight.survey.worker import (
    RCM_VERSION,
    SurveyExecutionResult,
    _child_environment,
    _collect_stage_timings,
    _emit_result_metrics,
    _heartbeat,
    _run_claimed_job,
    _run_metadata,
    execute_survey,
    process_survey_job,
    serve_survey_worker,
)
from scholight.survey.workflow_resources import WorkflowResourceError


@pytest.fixture(autouse=True)
def _stub_durable_progress_update() -> Iterator[AsyncMock]:
    """Worker unit tests do not initialize the PostgreSQL integration pool."""
    with patch(
        "scholight.survey.worker.update_survey_job_progress",
        new_callable=AsyncMock,
        return_value=True,
    ) as update:
        yield update


def test_worker_expects_pinned_rcm_release() -> None:
    assert RCM_VERSION == "0.2.12"


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
        survey_id=uuid4(),
        user_id=42,
        approved_draft_id=uuid4(),
        approved_draft="# Retrieval augmented generation\n\nStudy the field.",
        approved_draft_revision=2,
        client_request_id=uuid4(),
        request_hash="2" * 64,
        status=status,  # type: ignore[arg-type]
        terminal_outcome=outcome,  # type: ignore[arg-type]
        storage_prefix=None,
        storage_bucket=None,
        manifest_key=None,
        error_code=None,
        error_message=None,
        lease_owner=worker_id,
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


class _TimeoutProcess:
    stdout = None
    stderr = None
    stdin = None
    returncode: int | None = None

    def __init__(self) -> None:
        self.terminated = False
        self.stopped = asyncio.Event()

    async def wait(self) -> int:
        if not self.terminated:
            await self.stopped.wait()
        return -15

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True


class _CompletedProcess:
    def __init__(self, *, returncode: int = 0, stderr: bytes = b"") -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_data(stderr)
        self.stderr.feed_eof()
        self.stdin = None
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


@pytest.mark.asyncio
async def test_full_survey_retries_transient_provider_failure_from_clean_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_id = uuid4()
    job = _job(job_id=uuid4(), worker_id=worker_id, status="running")
    stale_artifact = tmp_path / "partial-model-output.md"
    stale_artifact.write_text("partial", encoding="utf-8")
    now = datetime.now(UTC)
    transient = SurveyExecutionResult(
        outcome="failed",
        error_code="survey_provider_unavailable",
        error_message="A Survey provider is temporarily unavailable.",
        started_at=now,
        finished_at=now,
    )
    succeeded = SurveyExecutionResult(
        outcome="succeeded",
        error_code=None,
        error_message=None,
        started_at=now,
        finished_at=now,
    )
    monkeypatch.setattr(settings, "survey_provider_max_attempts", 3)
    monkeypatch.setattr(settings, "survey_provider_retry_base_seconds", 2.0)
    monkeypatch.setattr(settings, "survey_provider_retry_max_seconds", 30.0)

    async def _execute_once(*args: object, **kwargs: object) -> SurveyExecutionResult:
        del args, kwargs
        if stale_artifact.exists():
            return transient
        return succeeded

    with (
        patch("scholight.survey.worker._execute_survey_once", side_effect=_execute_once) as execute,
        patch("scholight.survey.worker.asyncio.sleep", new_callable=AsyncMock) as sleep,
    ):
        result = await execute_survey(job, tmp_path)

    assert result.outcome == "succeeded"
    assert execute.await_count == 2
    assert not stale_artifact.exists()
    sleep.assert_awaited_once_with(2.0)


@pytest.mark.asyncio
async def test_zero_exit_incomplete_run_gets_one_same_workspace_repair(
    tmp_path: Path,
) -> None:
    job = _job(job_id=uuid4(), worker_id=uuid4(), status="running")
    partial = tmp_path / "partial-model-output.md"
    partial.write_text("keep for repair", encoding="utf-8")
    now = datetime.now(UTC)
    incomplete = SurveyExecutionResult(
        outcome="failed",
        error_code="survey_report_missing",
        error_message="Survey generation did not produce a final report.",
        started_at=now,
        finished_at=now,
        return_code=0,
        termination_reason="report_missing",
    )
    succeeded = SurveyExecutionResult(
        outcome="succeeded",
        error_code=None,
        error_message=None,
        started_at=now,
        finished_at=now,
        return_code=0,
        termination_reason="completed",
    )

    with patch(
        "scholight.survey.worker._execute_survey_once",
        new_callable=AsyncMock,
        side_effect=(incomplete, succeeded),
    ) as execute:
        result = await execute_survey(job, tmp_path)

    assert result.outcome == "succeeded"
    assert execute.await_count == 2
    assert partial.read_text(encoding="utf-8") == "keep for repair"


@pytest.mark.asyncio
async def test_missing_workflow_resources_fail_before_process_start(tmp_path: Path) -> None:
    create_process = AsyncMock()
    with (
        patch(
            "scholight.survey.worker.stage_workflow_schema",
            side_effect=WorkflowResourceError("missing schema"),
        ),
        patch(
            "scholight.survey.worker.asyncio.create_subprocess_exec",
            create_process,
        ),
    ):
        result = await execute_survey(
            _job(job_id=uuid4(), worker_id=uuid4(), status="running"),
            tmp_path,
        )

    assert result.error_code == "survey_workflow_resources_unavailable"
    assert result.termination_reason == "workflow_resources_unavailable"
    assert result.diagnostics is not None
    assert result.diagnostics["last_event"]["type"] == "run.finished"
    create_process.assert_not_awaited()


def _write_complete_workflow_artifacts(run_root: Path) -> None:
    for relative_path in {
        relative_path for contract in ARTIFACT_CONTRACTS for relative_path in contract.required
    }:
        path = run_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "[]" if path.suffix == ".json" else "observed"
        path.write_text(content, encoding="utf-8")
    (run_root / "00_outline.md").write_text(
        "# Survey Outline\n\n# Title\n\n**Survey**\n\n"
        "# Abstract\n\nA complete deterministic abstract.\n",
        encoding="utf-8",
    )
    sections = run_root / "sections"
    sections.mkdir(exist_ok=True)
    (sections / "01_introduction.md").write_text(
        "## 1. Introduction\n\nBody grounded in [2401.12345].\n",
        encoding="utf-8",
    )
    cards = run_root / "cards"
    cards.mkdir(exist_ok=True)
    (cards / "2401.12345.md").write_text(
        "# PaperCard\n\n## header\n\n"
        "- arxiv_id: 2401.12345\n"
        "- title: Paper\n"
        "- authors: Example\n"
        "- year/venue: 2024 arXiv\n",
        encoding="utf-8",
    )
    (run_root / "08_survey.md").unlink(missing_ok=True)
    (run_root / "index.md").unlink(missing_ok=True)


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

    async def _terminate(candidate: _TimeoutProcess) -> None:
        candidate.terminated = True
        candidate.returncode = -15
        candidate.stopped.set()

    with (
        patch(
            "scholight.survey.worker.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
        patch("scholight.survey.worker.write_stdin", new_callable=AsyncMock),
        patch("scholight.survey.worker.terminate_process_group", side_effect=_terminate),
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
async def test_archive_failure_is_exposed_as_retrying_not_terminal(tmp_path: Path) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    job_root = tmp_path / str(job_id)
    (job_root / "run").mkdir(parents=True)
    artifact_store = AsyncMock()
    artifact_store.archive_run.side_effect = OSError("temporary object-store failure")
    with (
        patch("scholight.survey.worker._job_root", return_value=job_root),
        patch("scholight.survey.worker._heartbeat", new_callable=AsyncMock),
        patch(
            "scholight.survey.worker.defer_survey_archive",
            new_callable=AsyncMock,
        ) as defer,
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

    call = defer.await_args
    assert call is not None
    assert call.kwargs["error_code"] == "survey_archive_pending"
    assert job_root.exists()


@pytest.mark.asyncio
async def test_success_requires_complete_final_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    _write_complete_workflow_artifacts(tmp_path)
    monkeypatch.setattr(settings, "survey_job_timeout_seconds", 60)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")
    monkeypatch.setattr(settings, "image_gen_api_key", "image")
    process = _CompletedProcess()
    with (
        patch(
            "scholight.survey.worker.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
        patch("scholight.survey.worker.write_stdin", new_callable=AsyncMock),
    ):
        result = await execute_survey(
            _job(job_id=job_id, worker_id=worker_id, status="running"),
            tmp_path,
        )

    assert result.outcome == "succeeded"


@pytest.mark.asyncio
async def test_zero_exit_missing_cited_card_is_finalization_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    _write_complete_workflow_artifacts(tmp_path)
    (tmp_path / "cards" / "2401.12345.md").unlink()
    monkeypatch.setattr(settings, "survey_job_timeout_seconds", 60)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")
    monkeypatch.setattr(settings, "image_gen_api_key", "image")
    process = _CompletedProcess()
    with (
        patch(
            "scholight.survey.worker.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
        patch("scholight.survey.worker.write_stdin", new_callable=AsyncMock),
    ):
        result = await execute_survey(
            _job(job_id=job_id, worker_id=worker_id, status="running"),
            tmp_path,
        )

    assert result.error_code == "survey_report_missing"


@pytest.mark.asyncio
async def test_deterministic_finalizer_includes_every_generated_section(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    _write_complete_workflow_artifacts(tmp_path)
    (tmp_path / "sections" / "02_research-arc.md").write_text(
        "## 2. Research Arc\n\nMore evidence [2401.12345].\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "survey_job_timeout_seconds", 60)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")
    monkeypatch.setattr(settings, "image_gen_api_key", "image")
    process = _CompletedProcess()
    with (
        patch(
            "scholight.survey.worker.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
        patch("scholight.survey.worker.write_stdin", new_callable=AsyncMock),
    ):
        result = await execute_survey(
            _job(job_id=job_id, worker_id=worker_id, status="running"),
            tmp_path,
        )

    assert result.outcome == "succeeded"
    assert "## 2. Research Arc" in (tmp_path / "08_survey.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_deterministic_finalizer_generates_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    _write_complete_workflow_artifacts(tmp_path)
    monkeypatch.setattr(settings, "survey_job_timeout_seconds", 60)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")
    monkeypatch.setattr(settings, "image_gen_api_key", "image")
    process = _CompletedProcess()
    with (
        patch(
            "scholight.survey.worker.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
        patch("scholight.survey.worker.write_stdin", new_callable=AsyncMock),
    ):
        result = await execute_survey(
            _job(job_id=job_id, worker_id=worker_id, status="running"),
            tmp_path,
        )

    assert result.outcome == "succeeded"
    report = (tmp_path / "08_survey.md").read_text(encoding="utf-8")
    assert "## References" in report
    assert "- [2401.12345]" in report


@pytest.mark.asyncio
async def test_zero_exit_missing_report_retains_runtime_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    monkeypatch.setattr(settings, "survey_job_timeout_seconds", 60)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")
    monkeypatch.setattr(settings, "image_gen_api_key", "image")
    process = _CompletedProcess(stderr=b"rank_pool completed without its output\n")
    with (
        patch(
            "scholight.survey.worker.asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ),
        patch("scholight.survey.worker.write_stdin", new_callable=AsyncMock),
    ):
        result = await execute_survey(
            _job(job_id=job_id, worker_id=worker_id, status="running"),
            tmp_path,
        )

    assert result.error_code == "survey_report_missing"
    assert result.return_code == 0
    assert result.stderr_tail == "rank_pool completed without its output\n"
    assert result.diagnostics is not None
    assert result.diagnostics["first_anomaly"] is not None


def test_run_metadata_v2_contains_process_and_diagnostic_summary(tmp_path: Path) -> None:
    job_id = uuid4()
    worker_id = uuid4()
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=job_id,
        survey_id=uuid4(),
    )
    diagnostics.record("run.started")
    result = SurveyExecutionResult(
        outcome="failed",
        error_code="survey_report_missing",
        error_message="Survey generation did not produce a final report.",
        started_at=datetime.now(UTC),
        finished_at=datetime.now(UTC),
        return_code=0,
        termination_reason="report_missing",
        stderr_tail="bounded diagnostics",
        diagnostics=diagnostics.snapshot(),
    )

    metadata = _run_metadata(
        _job(job_id=job_id, worker_id=worker_id, status="archiving", outcome="failed"),
        result,
    )

    assert metadata["schema_version"] == 2
    assert metadata["process"] == {
        "return_code": 0,
        "termination_reason": "report_missing",
        "stderr_tail": "bounded diagnostics",
    }
    assert metadata["diagnostics"]["trace_path"] == "trajectory.jsonl"  # type: ignore[index]


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
    assert settled.kwargs["error_code"] == "survey_runtime_unavailable"
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


@pytest.mark.asyncio
async def test_stage_collector_records_contract_breach_without_stopping(tmp_path: Path) -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(
        b'{"type":"component_start","name":"discovery_merger","kind":"accelerator","index":1}\n'
        b'{"type":"component_done","name":"discovery_merger","kind":"accelerator","index":1}\n'
        b'{"type":"component_start","name":"expansion","kind":"accelerator","index":2}\n'
    )
    stream.feed_eof()
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    records = await _collect_stage_timings(stream, diagnostics=diagnostics)

    assert len(records) == 1
    assert diagnostics.snapshot()["first_anomaly"]["component"] == "discovery_merger"
    assert diagnostics.snapshot()["last_event"]["component"] == "expansion"


@pytest.mark.asyncio
async def test_stage_collector_understands_native_rcm_tool_events(tmp_path: Path) -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(
        b'{"type":"tool_call","tool":"scholight__search_papers",'
        b'"call_id":"call-1","arguments":{"query":"rag","limit":10}}\n'
        b'{"type":"tool_result","tool":"scholight__search_papers",'
        b'"call_id":"call-1","duration":0.25,"result_len":1}\n'
    )
    stream.feed_eof()
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    await _collect_stage_timings(stream, diagnostics=diagnostics)

    assert diagnostics.snapshot()["tool_counts"] == {"started": 1, "finished": 1, "failed": 0}


@pytest.mark.asyncio
async def test_stage_collector_classifies_model_timeout_without_persisting_content(
    tmp_path: Path,
) -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(
        b'{"type":"completion_start"}\n'
        b'{"type":"completion_end","fragments":1,"input_tokens":0,'
        b'"output_tokens":0,"total_tokens":0}\n'
        b'{"type":"appended","id":7,"step":4,"role":"assistant",'
        b'"kind":"hitch","tag":"error",'
        b'"preview":"request timed out after 180s bearer private-secret"}\n'
    )
    stream.feed_eof()
    diagnostics = SurveyDiagnostics(
        run_root=tmp_path,
        job_id=uuid4(),
        survey_id=uuid4(),
    )

    await _collect_stage_timings(stream, diagnostics=diagnostics)

    snapshot = diagnostics.snapshot()
    assert snapshot["model_counts"] == {"started": 1, "finished": 1, "failed": 1}
    assert snapshot["last_model_error"] == {
        "error_code": "model_timeout",
        "timeout_seconds": 180,
    }
    trace = (tmp_path / "trajectory.jsonl").read_text(encoding="utf-8")
    assert "private-secret" not in trace
    assert "request timed out" not in trace


@pytest.mark.asyncio
async def test_stage_collector_persists_only_public_milestone_transitions() -> None:
    stream = asyncio.StreamReader()
    stream.feed_data(
        b'{"type":"component_start","name":"discovery","kind":"accelerator","index":1}\n'
        b'{"type":"component_start","name":"method_scout","kind":"accelerator","index":2}\n'
        b'{"type":"component_start","name":"PaperCard","kind":"subworkflow","index":3}\n'
        b'{"type":"component_start","name":"internal_unknown","kind":"accelerator","index":4}\n'
    )
    stream.feed_eof()
    job_id = uuid4()
    worker_id = uuid4()

    with patch(
        "scholight.survey.worker.update_survey_job_progress",
        new_callable=AsyncMock,
        return_value=True,
    ) as update:
        await _collect_stage_timings(stream, job_id=job_id, worker_id=worker_id)

    assert [call.kwargs["stage"] for call in update.await_args_list] == [
        "discovering",
        "reviewing_evidence",
    ]


def test_child_environment_exposes_only_required_provider_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHOLIGHT_PG_PASSWORD", "must-not-reach-rcm")
    monkeypatch.setenv("SCHOLIGHT_ZILLIZ_TOKEN", "must-not-reach-rcm")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-reach-rcm")
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek")
    monkeypatch.setattr(settings, "image_gen_api_key", "image")

    job_id = uuid4()
    environment = _child_environment(user_id=42, job_id=job_id)

    assert environment["DEEPSEEK_API_KEY"] == "deepseek"
    assert environment["IMAGE_GEN_API_KEY"] == "image"
    assert environment["SCHOLIGHT_SURVEY_MCP_AUTHORIZATION"].startswith("Bearer ")
    assert "SCHOLIGHT_PG_PASSWORD" not in environment
    assert "SCHOLIGHT_ZILLIZ_TOKEN" not in environment
    assert "AWS_SECRET_ACCESS_KEY" not in environment
    claims = jwt.decode(
        environment["SCHOLIGHT_SURVEY_MCP_AUTHORIZATION"].removeprefix("Bearer "),
        "s" * 32,
        algorithms=["HS256"],
        audience="scholight-mcp",
        issuer="scholight-survey",
    )
    assert claims["survey_job_id"] == str(job_id)


def test_result_metrics_use_only_low_cardinality_dimensions() -> None:
    now = datetime.now(UTC)
    result = SurveyExecutionResult(
        outcome="failed",
        error_code="survey_report_missing",
        error_message="missing",
        started_at=now,
        finished_at=now,
        diagnostics={
            "anomalies": [{"severity": "error"}, {"severity": "warning"}],
            "tool_counts": {"failed": 2},
            "write_failure_count": 1,
            "last_activity_at": now.isoformat(),
        },
    )

    with patch("scholight.survey.worker.emit_emf") as emit:
        _emit_result_metrics(result)

    assert emit.call_count == 2
    outcome_call, failure_call = emit.call_args_list
    assert outcome_call.kwargs == {
        "service": "survey-worker",
        "outcome": "failed",
        "metrics": {
            "SurveyJobCount": (1, "Count"),
            "SurveyJobDuration": (0, "Milliseconds"),
        },
    }
    assert failure_call.kwargs["service"] == "survey-worker"
    assert "outcome" not in failure_call.kwargs
    assert "job_id" not in failure_call.kwargs
    assert failure_call.kwargs["metrics"] == {
        "SurveyContractAnomaly": (1, "Count"),
        "SurveyRuntimeFailure": (1, "Count"),
        "SurveyProviderThrottled": (0, "Count"),
        "SurveyToolFailure": (2, "Count"),
        "SurveyDiagnosticsWriteFailure": (1, "Count"),
        "SurveyLastActivityAge": (0, "Seconds"),
    }


@pytest.mark.asyncio
async def test_job_heartbeat_database_failure_stops_work_after_lease_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = ProcessControl()
    monkeypatch.setattr(settings, "survey_heartbeat_seconds", 0.001)
    monkeypatch.setattr(settings, "survey_lease_seconds", 0)

    with (
        patch(
            "scholight.survey.worker.heartbeat_survey_job",
            new_callable=AsyncMock,
            side_effect=OSError("database unavailable"),
        ) as heartbeat,
    ):
        await asyncio.wait_for(
            _heartbeat(
                job_id=uuid4(),
                worker_id=uuid4(),
                stop=asyncio.Event(),
                control=control,
            ),
            timeout=1,
        )

    heartbeat.assert_awaited_once()
    assert control.lease_lost.is_set()


@pytest.mark.asyncio
async def test_job_heartbeat_stops_process_after_cancellation_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = ProcessControl()
    monkeypatch.setattr(settings, "survey_heartbeat_seconds", 0.001)
    monkeypatch.setattr(settings, "survey_lease_seconds", 30)

    with patch(
        "scholight.survey.worker.heartbeat_survey_job",
        new_callable=AsyncMock,
        return_value="cancel_requested",
    ) as heartbeat:
        await asyncio.wait_for(
            _heartbeat(
                job_id=uuid4(),
                worker_id=uuid4(),
                stop=asyncio.Event(),
                control=control,
            ),
            timeout=1,
        )

    heartbeat.assert_awaited_once()
    assert control.cancel_requested.is_set()


@pytest.mark.asyncio
async def test_survey_supervisor_keeps_execution_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = [_job(job_id=uuid4(), worker_id=uuid4(), status="running") for _ in range(4)]
    running = 0
    maximum_running = 0
    started = 0
    at_capacity = asyncio.Event()
    all_started = asyncio.Event()
    release = asyncio.Event()
    monkeypatch.setattr(settings, "survey_job_worker_concurrency", 2)
    monkeypatch.setattr(settings, "survey_job_global_concurrency", 16)
    monkeypatch.setattr(settings, "survey_job_per_user_concurrency", 1)

    async def _claim(**kwargs: object) -> SurveyJob | None:
        del kwargs
        return jobs.pop(0) if jobs else None

    async def _run(**kwargs: object) -> None:
        nonlocal running, maximum_running, started
        del kwargs
        running += 1
        started += 1
        maximum_running = max(maximum_running, running)
        if running == 2:
            at_capacity.set()
        if started == 4:
            all_started.set()
        try:
            await release.wait()
        finally:
            running -= 1

    async def _cleanup() -> None:
        await asyncio.Event().wait()

    async def _email_notifications(_sender: object) -> None:
        await asyncio.Event().wait()

    with (
        patch("scholight.survey.worker.claim_survey_job", side_effect=_claim),
        patch("scholight.survey.worker._run_claimed_job", side_effect=_run),
        patch(
            "scholight.survey.worker.recover_expired_survey_jobs",
            new_callable=AsyncMock,
        ),
        patch("scholight.survey.worker.serve_artifact_cleanup", side_effect=_cleanup),
        patch(
            "scholight.survey.worker.serve_email_notifications",
            side_effect=_email_notifications,
        ),
        patch("scholight.survey.worker.AliyunSurveyEmailSender") as sender_factory,
        patch("scholight.survey.worker.SurveyArtifactStore"),
        patch("scholight.survey.worker._IDLE_SECONDS", 0.001),
    ):
        injected_sender = AsyncMock()
        supervisor = asyncio.create_task(serve_survey_worker(email_sender=injected_sender))
        await asyncio.wait_for(at_capacity.wait(), timeout=1)
        assert running == 2
        release.set()
        await asyncio.wait_for(all_started.wait(), timeout=1)
        supervisor.cancel()
        with pytest.raises(asyncio.CancelledError):
            await supervisor

    assert maximum_running == 2
    sender_factory.assert_not_called()


@pytest.mark.asyncio
async def test_survey_task_failure_does_not_escape_supervisor_boundary() -> None:
    job = _job(job_id=uuid4(), worker_id=uuid4(), status="running")
    with patch(
        "scholight.survey.worker.process_survey_job",
        new_callable=AsyncMock,
        side_effect=RuntimeError("isolated failure"),
    ):
        await _run_claimed_job(
            job=job,
            worker_id=uuid4(),
            artifact_store=AsyncMock(),
        )
