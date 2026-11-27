"""New failure diagnostics expose fixed categories, never paper or provider data."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import grpc
import httpx
import pytest

from scholight.config import settings
from scholight.db import ingestion
from scholight.db.client import DBError
from scholight.db.queries_ingestion import IngestionJob
from scholight.models.ingestion_target import IngestionTarget
from scholight.pipeline.latex_md import LatexMdError
from scholight.pipeline.pdf_md import PDFMdError
from scholight.scheduler import ingest_worker as worker
from scholight.scheduler.resources import ResourceCorruptError, ResourceUnavailableError
from scholight.store.ingestion import IngestionSafetyError

_SECRET = "SENSITIVE-KEY-BODY-HEADER-PAPER-CONTENT"
_EVENTS = {"ingestion stage failed", "ingestion attempt failed"}


def _http_error(response: object | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request(
        "POST",
        f"https://test.invalid/{_SECRET}?token={_SECRET}",
        headers={"Authorization": f"Bearer {_SECRET}"},
        content=_SECRET,
    )
    if response is None:
        response = httpx.Response(
            402, request=request, content=_SECRET, headers={"X-Private": _SECRET}
        )
    return httpx.HTTPStatusError(_SECRET, request=request, response=cast(httpx.Response, response))


class FakeRpcError(grpc.RpcError):  # type: ignore[misc]
    def __init__(self, *, broken_code: bool = False) -> None:
        super().__init__()
        self.broken_code = broken_code

    def code(self) -> grpc.StatusCode:
        if self.broken_code:
            raise RuntimeError(_SECRET)
        return grpc.StatusCode.DEADLINE_EXCEEDED

    def details(self) -> str:
        raise AssertionError("Provider details must not be read")

    def __str__(self) -> str:
        raise AssertionError("RPC message must not be rendered")


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    target = IngestionTarget("https://test.invalid", "papers", "chunks", "unit-test", 4)
    monkeypatch.setattr(settings, "zilliz_uri", target.endpoint)
    monkeypatch.setattr(settings, "embedding_model", target.embedding_model)
    monkeypatch.setattr(settings, "embedding_dim", target.embedding_dim)
    monkeypatch.setattr(settings, "ingestion_target_id", target.key)
    job = IngestionJob(
        arxiv_id="2401.00001",
        target_version=1,
        source="new",
        priority=10,
        status="running",
        attempt_count=1,
        max_attempts=8,
        lease_owner="worker",
    )
    queue = SimpleNamespace(
        target_id=target.key,
        sync_source=AsyncMock(return_value="arxiv"),
        claim=AsyncMock(return_value=job),
        renew=AsyncMock(return_value=True),
        release=AsyncMock(return_value=True),
        complete=AsyncMock(),
        fail=AsyncMock(),
    )
    describe = Mock(
        side_effect=lambda name, **_kwargs: {
            "collection_id": "papers" if name == "arxiv_papers" else "chunks"
        }
    )
    logger, emit = Mock(), Mock()
    process = AsyncMock(return_value="installed")
    monkeypatch.setattr(ingestion, "configured_queue", lambda: queue)
    monkeypatch.setattr(worker, "configured_queue", lambda: queue)
    monkeypatch.setattr(
        ingestion, "get_client", Mock(return_value=SimpleNamespace(describe_collection=describe))
    )
    monkeypatch.setattr(worker, "process_job", process)
    monkeypatch.setattr(worker, "logger", logger)
    monkeypatch.setattr(worker, "emit_emf", emit)
    return SimpleNamespace(job=job, queue=queue, process=process, logger=logger, emit=emit)


def _new_events(logger: Mock) -> list[tuple[str, dict[str, object]]]:
    events = [
        (call.args[0], dict(call.kwargs))
        for call in logger.method_calls
        if call.args and call.args[0] in _EVENTS
    ]
    assert _SECRET not in str(events)
    assert "2401.00001" not in str(events)
    for event, fields in events:
        allowed = {"error_category", "http_status", "grpc_status", "vector_status"}
        allowed |= {"stage"} if event == "ingestion stage failed" else {"failure_code", "outcome"}
        assert set(fields) <= allowed
        assert "error_category" in fields
    return events


def test_http_status_diagnostics_discard_all_provider_and_paper_data() -> None:
    assert worker._safe_failure_fields(_http_error()) == {
        "error_category": "http_status",
        "http_status": 402,
    }


def test_unknown_sensitive_exception_class_name_is_other() -> None:
    def forbidden_render(_self: Exception) -> str:
        raise AssertionError("Unknown exception rendering must not be called")

    error_type = type(
        _SECRET, (Exception,), {"__str__": forbidden_render, "__repr__": forbidden_render}
    )
    error = error_type(_SECRET)
    error.__cause__ = RuntimeError(_SECRET)
    assert worker._safe_failure_fields(error) == {"error_category": "other"}


@pytest.mark.parametrize("broken_code", [False, True])
def test_rpc_diagnostics_only_read_structured_status(broken_code: bool) -> None:
    expected = {"error_category": "grpc"}
    if not broken_code:
        expected["grpc_status"] = "DEADLINE_EXCEEDED"
    assert worker._safe_failure_fields(FakeRpcError(broken_code=broken_code)) == expected


@pytest.mark.parametrize(
    ("error", "category"),
    [
        (httpx.ReadTimeout(_SECRET), "http_timeout"),
        (httpx.ConnectError(_SECRET), "http_transport"),
        (worker.InvalidIngestionJobError(_SECRET), "invalid_job"),
        (IngestionSafetyError(_SECRET), "invalid_job"),
        (ResourceUnavailableError(_SECRET), "source_or_parse"),
        (ResourceCorruptError(_SECRET), "source_or_parse"),
        (LatexMdError(_SECRET), "source_or_parse"),
        (PDFMdError(_SECRET), "source_or_parse"),
        (ValueError(_SECRET), "value_error"),
        (KeyError(_SECRET), "key_error"),
        (TypeError(_SECRET), "type_error"),
        (TimeoutError(_SECRET), "timeout"),
        (RuntimeError(_SECRET), "runtime_error"),
        (OSError(_SECRET), "other"),
    ],
)
def test_exception_categories_are_fixed(error: Exception, category: str) -> None:
    assert worker._safe_failure_fields(error) == {"error_category": category}


def test_stage_preserves_original_error_when_status_metadata_is_broken(
    boundary: SimpleNamespace,
) -> None:
    class BrokenStatus:
        @property
        def status_code(self) -> int:
            raise RuntimeError(_SECRET)

    error = _http_error(BrokenStatus())
    with pytest.raises(httpx.HTTPStatusError) as caught, worker._stage(boundary.job, "embedding"):
        raise error
    assert caught.value is error
    assert _new_events(boundary.logger) == [
        ("ingestion stage failed", {"stage": "embedding", "error_category": "http_status"})
    ]


def test_stage_name_is_allowlisted(boundary: SimpleNamespace) -> None:
    with pytest.raises(ValueError), worker._stage(boundary.job, _SECRET):
        raise ValueError(_SECRET)
    assert _new_events(boundary.logger) == [
        ("ingestion stage failed", {"stage": "other", "error_category": "value_error"})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(("attempt_count", "outcome"), [(1, "retry"), (8, "dead")])
async def test_nested_stage_diagnostics_do_not_duplicate_attempt_outcome(
    boundary: SimpleNamespace,
    attempt_count: int,
    outcome: str,
) -> None:
    error = _http_error()
    job = replace(boundary.job, attempt_count=attempt_count)
    boundary.queue.claim.return_value = job

    async def process(*_args: object, **_kwargs: object) -> str:
        with worker._stage(job, "prepare_including_embedding"), worker._stage(job, "embedding"):
            raise error

    boundary.process.side_effect = process
    assert await worker.run_worker_once("worker", max_processing_seconds=1)
    events = _new_events(boundary.logger)
    stages = [fields for name, fields in events if name == "ingestion stage failed"]
    attempts = [fields for name, fields in events if name == "ingestion attempt failed"]
    assert [fields["stage"] for fields in stages] == ["embedding", "prepare_including_embedding"]
    assert all(fields["http_status"] == 402 for fields in stages)
    assert attempts == [
        {
            "failure_code": "temporary_failure",
            "outcome": outcome,
            "error_category": "http_status",
            "http_status": 402,
        }
    ]
    boundary.queue.fail.assert_awaited_once()
    assert boundary.queue.fail.await_args.kwargs["code"] == "temporary_failure"
    assert (boundary.queue.fail.await_args.kwargs["retry_at"] is not None) == (outcome == "retry")
    boundary.queue.complete.assert_not_awaited()
    assert not any("IngestionSucceeded" in str(call) for call in boundary.emit.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "code", "outcome"),
    [
        (worker.InvalidIngestionJobError(_SECRET), "invalid_job", "dead"),
        (ResourceCorruptError(_SECRET), "source_or_parse_failure", "retry"),
    ],
)
async def test_final_failure_diagnostic_preserves_existing_outcome(
    boundary: SimpleNamespace,
    error: Exception,
    code: str,
    outcome: str,
) -> None:
    boundary.process.side_effect = error
    assert await worker.run_worker_once("worker", max_processing_seconds=1)
    events = _new_events(boundary.logger)
    assert len(events) == 1
    assert events[0][0] == "ingestion attempt failed"
    assert events[0][1]["failure_code"] == code
    assert events[0][1]["outcome"] == outcome
    boundary.queue.complete.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_state_save_does_not_log_an_uncommitted_outcome(
    boundary: SimpleNamespace,
) -> None:
    boundary.process.side_effect = _http_error()
    boundary.queue.fail.side_effect = DBError("synthetic save failed")
    with pytest.raises(DBError, match="synthetic save failed"):
        await worker.run_worker_once("worker", max_processing_seconds=1)
    assert _new_events(boundary.logger) == []
    boundary.queue.complete.assert_not_awaited()
    assert not any(
        "IngestionRetry" in str(call) or "IngestionDead" in str(call)
        for call in boundary.emit.call_args_list
    )


@pytest.mark.asyncio
async def test_success_does_not_emit_new_failure_diagnostics(boundary: SimpleNamespace) -> None:
    assert await worker.run_worker_once("worker", max_processing_seconds=1)
    boundary.queue.complete.assert_awaited_once_with("2401.00001", "worker")
    boundary.queue.fail.assert_not_awaited()
    assert _new_events(boundary.logger) == []


@pytest.mark.asyncio
async def test_cancellation_releases_without_new_failure_diagnostics(
    boundary: SimpleNamespace,
) -> None:
    started = asyncio.Event()

    async def process(*_args: object, **_kwargs: object) -> str:
        with worker._stage(boundary.job, "embedding"):
            started.set()
            await asyncio.Event().wait()
        return "installed"

    boundary.process.side_effect = process
    task = asyncio.create_task(worker.run_worker_once("worker", max_processing_seconds=1))
    await asyncio.wait_for(started.wait(), 0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    boundary.queue.release.assert_awaited_once_with("2401.00001", "worker")
    boundary.queue.fail.assert_not_awaited()
    boundary.queue.complete.assert_not_awaited()
    assert _new_events(boundary.logger) == []
