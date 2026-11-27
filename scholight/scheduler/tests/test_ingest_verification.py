"""Target preflight failures must never become paper outcomes or idle success."""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import grpc
import pytest

from scholight.config import settings
from scholight.db import ingestion
from scholight.db.client import DBError
from scholight.db.queries_ingestion import IngestionJob
from scholight.models.ingestion_target import IngestionTarget
from scholight.scheduler import ingest_worker as worker


class RpcFailure(grpc.RpcError):  # type: ignore[misc]  # grpc.RpcError has no typing stubs.
    """Structured RPC status without a real connection or provider response."""

    def __init__(self, status: grpc.StatusCode) -> None:
        super().__init__()
        self.status = status

    def code(self) -> grpc.StatusCode:
        return self.status

    def __str__(self) -> str:
        return "never-log-provider-token-or-body"


@pytest.fixture
def boundary(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Exercise the real worker/facade/target code, replacing external boundaries."""
    target = IngestionTarget("https://test.invalid", "papers", "chunks", "unit-test", 4)
    monkeypatch.setattr(settings, "zilliz_uri", target.endpoint)
    monkeypatch.setattr(settings, "embedding_model", target.embedding_model)
    monkeypatch.setattr(settings, "embedding_dim", target.embedding_dim)
    monkeypatch.setattr(settings, "ingestion_target_id", target.key)
    monkeypatch.setattr(settings, "ingest_concurrency", 1)
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
        status=AsyncMock(
            return_value={"queue": {"backlog": 1, "dead": 0, "oldest_age_seconds": 0}}
        ),
    )
    describe = Mock(
        side_effect=lambda name, **_kwargs: {
            "collection_id": "papers" if name == "arxiv_papers" else "chunks"
        }
    )
    client = SimpleNamespace(describe_collection=describe)
    connect = Mock(return_value=client)
    process = AsyncMock(return_value="installed")
    logger = Mock()
    emit = Mock()
    monkeypatch.setattr(ingestion, "configured_queue", lambda: queue)
    monkeypatch.setattr(worker, "configured_queue", lambda: queue)
    monkeypatch.setattr(ingestion, "get_client", connect)
    monkeypatch.setattr(worker, "process_job", process)
    monkeypatch.setattr(worker, "logger", logger)
    monkeypatch.setattr(worker, "emit_emf", emit)
    # Short delays keep the real control flow testable without a long sleep.
    monkeypatch.setattr(worker, "_TARGET_VERIFICATION_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(worker, "_TARGET_VERIFICATION_RPC_TIMEOUT", 5.0, raising=False)
    monkeypatch.setattr(worker, "_TARGET_VERIFICATION_BUDGET", 45.0, raising=False)
    monkeypatch.setattr(worker, "_TARGET_VERIFICATION_BACKOFF", 0.001, raising=False)
    return SimpleNamespace(
        queue=queue, describe=describe, connect=connect, process=process, logger=logger, emit=emit
    )


def assert_no_paper_outcome(boundary: SimpleNamespace) -> None:
    boundary.queue.claim.assert_not_awaited()
    boundary.queue.complete.assert_not_awaited()
    boundary.queue.fail.assert_not_awaited()
    boundary.queue.release.assert_not_awaited()
    boundary.process.assert_not_awaited()
    assert not any(
        "IngestionSucceeded" in str(call) or "IngestionDead" in str(call)
        for call in boundary.emit.call_args_list
    )


@pytest.mark.asyncio
async def test_target_deadline_retry_recovers_before_claim(boundary: SimpleNamespace) -> None:
    boundary.describe.side_effect = [
        RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED),
        {"collection_id": "papers"},
        {"collection_id": "chunks"},
    ]
    assert await worker.run_worker_once("worker", max_processing_seconds=1)
    assert boundary.describe.call_count == 3
    boundary.queue.claim.assert_awaited_once()
    boundary.queue.complete.assert_awaited_once_with("2401.00001", "worker")
    boundary.queue.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_target_deadline_retry_exhaustion_is_dependency_failure(
    boundary: SimpleNamespace,
) -> None:
    boundary.describe.side_effect = RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED)
    error_type = getattr(worker, "IngestionTargetUnavailableError", RuntimeError)
    with pytest.raises(error_type):
        await worker.run_worker_once("worker", max_processing_seconds=1)
    assert boundary.describe.call_count == 3
    assert_no_paper_outcome(boundary)
    calls = boundary.logger.method_calls
    assert any(call.kwargs.get("outcome") == "dependency_unavailable" for call in calls)
    assert "never-log-provider-token-or-body" not in str(calls)


@pytest.mark.asyncio
async def test_wrong_target_remains_fatal_without_retry(boundary: SimpleNamespace) -> None:
    boundary.describe.side_effect = lambda *_args, **_kwargs: {"collection_id": "wrong"}
    with pytest.raises(DBError, match="identity"):
        await worker.run_worker_once("worker", max_processing_seconds=1)
    assert boundary.describe.call_count == 2
    boundary.queue.sync_source.assert_not_awaited()
    assert_no_paper_outcome(boundary)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        grpc.StatusCode.PERMISSION_DENIED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.UNAVAILABLE,
    ],
)
async def test_non_deadline_rpc_error_is_not_retried(
    boundary: SimpleNamespace,
    status: grpc.StatusCode,
) -> None:
    boundary.describe.side_effect = RpcFailure(status)
    with pytest.raises(RpcFailure):
        await worker.run_worker_once("worker", max_processing_seconds=1)
    assert boundary.describe.call_count == 1
    assert_no_paper_outcome(boundary)


@pytest.mark.asyncio
async def test_target_rpc_timeouts_receive_remaining_budget(boundary: SimpleNamespace) -> None:
    deadline = time.monotonic() + 0.05
    assert await ingestion.verified_sync_source(deadline=deadline, rpc_timeout=5.0) == "arxiv"
    assert 0 < boundary.connect.call_args.kwargs["timeout"] <= 0.05
    calls = boundary.describe.call_args_list
    assert len(calls) == 2
    assert all(0 < call.kwargs["timeout"] <= 0.05 for call in calls)


@pytest.mark.asyncio
async def test_expired_verification_deadline_never_connects(boundary: SimpleNamespace) -> None:
    with pytest.raises(TimeoutError):
        await ingestion.verified_sync_source(deadline=time.monotonic() - 1, rpc_timeout=5.0)
    boundary.connect.assert_not_called()
    boundary.queue.sync_source.assert_not_awaited()


@pytest.mark.asyncio
async def test_preflight_time_is_subtracted_from_processing_slot(boundary: SimpleNamespace) -> None:
    def describe(name: str, **_kwargs: object) -> dict[str, str]:
        time.sleep(0.015)
        return {"collection_id": "papers" if name == "arxiv_papers" else "chunks"}

    async def process(*_args: object, **_kwargs: object) -> str:
        await asyncio.sleep(0.04)
        return "installed"

    boundary.describe.side_effect = describe
    boundary.process.side_effect = process
    await worker.run_worker_once("worker", max_processing_seconds=0.05)
    boundary.queue.release.assert_awaited_once_with("2401.00001", "worker")
    boundary.queue.complete.assert_not_awaited()
    boundary.queue.fail.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("rpc_fails_after_cancel", [False, True])
async def test_cancellation_waits_for_verification_thread_to_finish(
    boundary: SimpleNamespace,
    rpc_fails_after_cancel: bool,
) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()

    def describe(name: str, **_kwargs: object) -> dict[str, str]:
        loop.call_soon_threadsafe(started.set)
        try:
            assert release.wait(0.5)
            if rpc_fails_after_cancel:
                raise RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED)
            return {"collection_id": "papers" if name == "arxiv_papers" else "chunks"}
        finally:
            finished.set()

    boundary.describe.side_effect = describe
    task = asyncio.create_task(worker.run_worker_once("worker", max_processing_seconds=1))
    try:
        await asyncio.wait_for(started.wait(), 0.5)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished.is_set()
        assert_no_paper_outcome(boundary)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_interrupts_verification_backoff(
    boundary: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    failed = asyncio.Event()
    stop = asyncio.Event()
    monkeypatch.setattr(worker, "_TARGET_VERIFICATION_BACKOFF", 0.05)

    def describe(*_args: object, **_kwargs: object) -> dict[str, str]:
        loop.call_soon_threadsafe(failed.set)
        raise RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED)

    boundary.describe.side_effect = describe
    task = asyncio.create_task(
        worker.run_worker_once("worker", stop_event=stop, max_processing_seconds=1)
    )
    await asyncio.wait_for(failed.wait(), 0.5)
    await asyncio.sleep(0.002)
    stop.set()
    await asyncio.wait_for(task, 0.03)
    assert boundary.describe.call_count == 1
    assert_no_paper_outcome(boundary)


@pytest.mark.asyncio
async def test_cancel_interrupts_verification_backoff(
    boundary: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    failed = asyncio.Event()
    monkeypatch.setattr(worker, "_TARGET_VERIFICATION_BACKOFF", 0.05)

    def describe(*_args: object, **_kwargs: object) -> dict[str, str]:
        loop.call_soon_threadsafe(failed.set)
        raise RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED)

    boundary.describe.side_effect = describe
    task = asyncio.create_task(worker.run_worker_once("worker", max_processing_seconds=1))
    await asyncio.wait_for(failed.wait(), 0.5)
    await asyncio.sleep(0.002)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert boundary.describe.call_count == 1
    assert_no_paper_outcome(boundary)


@pytest.mark.asyncio
async def test_stop_after_successful_verification_prevents_claim(boundary: SimpleNamespace) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def describe(name: str, **_kwargs: object) -> dict[str, str]:
        if name == "arxiv_chunks":
            loop.call_soon_threadsafe(stop.set)
        return {"collection_id": "papers" if name == "arxiv_papers" else "chunks"}

    boundary.describe.side_effect = describe
    await worker.run_worker_once("worker", stop_event=stop, max_processing_seconds=1)
    assert boundary.describe.call_count == 2
    assert_no_paper_outcome(boundary)


@pytest.mark.asyncio
async def test_stop_during_claim_releases_without_processing(boundary: SimpleNamespace) -> None:
    stop = asyncio.Event()
    job = cast("IngestionJob", boundary.queue.claim.return_value)

    async def claim(*_args: object, **_kwargs: object) -> IngestionJob:
        stop.set()
        return job

    boundary.queue.claim.side_effect = claim
    await worker.run_worker_once("worker", stop_event=stop, max_processing_seconds=1)
    boundary.queue.release.assert_awaited_once_with("2401.00001", "worker")
    boundary.process.assert_not_awaited()
    boundary.queue.complete.assert_not_awaited()
    boundary.queue.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_worker_slot_does_not_connect_or_claim(boundary: SimpleNamespace) -> None:
    await worker.run_worker_once("worker", max_processing_seconds=0)
    boundary.connect.assert_not_called()
    assert_no_paper_outcome(boundary)


@pytest.mark.asyncio
async def test_slot_expiring_during_verification_does_not_claim(boundary: SimpleNamespace) -> None:
    def describe(name: str, **_kwargs: object) -> dict[str, str]:
        time.sleep(0.01)
        return {"collection_id": "papers" if name == "arxiv_papers" else "chunks"}

    boundary.describe.side_effect = describe
    await worker.run_worker_once("worker", max_processing_seconds=0.005)
    assert boundary.describe.call_count == 1
    assert_no_paper_outcome(boundary)


@pytest.mark.asyncio
async def test_real_drain_does_not_turn_exhausted_preflight_into_idle(
    boundary: SimpleNamespace,
) -> None:
    boundary.describe.side_effect = RpcFailure(grpc.StatusCode.DEADLINE_EXCEEDED)
    with pytest.raises(ExceptionGroup) as caught:
        await worker.drain_ingest(
            concurrency=1,
            stop_event=asyncio.Event(),
            max_runtime_seconds=1,
            idle_grace_seconds=0.01,
        )
    error_type = getattr(worker, "IngestionTargetUnavailableError", RuntimeError)
    assert any(isinstance(error, error_type) for error in caught.value.exceptions)
    assert boundary.describe.call_count == 3
    assert_no_paper_outcome(boundary)
    assert not any(
        call.args and call.args[0] == "ingestion drain finished"
        for call in boundary.logger.method_calls
    )
