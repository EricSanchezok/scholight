from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from observe import completion_summary, sanitize

from scholight.web_extract.admission import BoundedGate
from scholight.web_extract.memory import MemoryGuard, MemorySample


def event(request_id="natural", scope="rest", pagination=False):
    return {
        "event": "extract_completed",
        "request_id": request_id,
        "scope": scope,
        "pagination": pagination,
        "outcome": "initial_success",
        "mime": "html",
        "render_mode": "auto",
        "duration_ms": 12,
    }


def test_observation_counts_natural_first_extractions_without_internal_duplicates():
    rows = [event(), event(scope="internal"), event(pagination=True), event("canary")]
    report = completion_summary(rows, {"canary"})
    assert report["natural_public_initial"]["requests"] == 1
    assert report["natural_public_pagination"]["requests"] == 1
    assert report["canary"]["requests"] == 1


def test_observation_keeps_errors_and_rejections_in_rate_denominator():
    success, rejected = event(), event()
    rejected["outcome"] = "error_extract_capacity_exceeded"
    summary = completion_summary([success, rejected], set())["natural_public_initial"]
    assert summary["success_rate"] == 0.5
    assert summary["rejection_rate"] == 0.5


def test_observation_allowlist_drops_urls_and_secret_or_unknown_fields():
    row = event()
    row.update(url="https://private.example", headers={"Authorization": "secret"}, custom="secret")
    result = sanitize({"timestamp": 1234, "eventId": "id", "logStreamName": "stream"}, row)
    assert "private" not in json.dumps(result)
    assert "secret" not in json.dumps(result)
    assert result["request_id"] == "natural"


@pytest.mark.asyncio
async def test_observer_retains_actual_producer_queue_metrics():
    for stage in ("Download", "Parse", "Browser"):
        gate = BoundedGate(stage, capacity=1, max_waiters=1, wait_seconds=1)
        with patch("scholight.web_extract.admission.emit_emf") as emit:
            await gate.acquire()
            gate.release()
        produced = {key: value[0] for key, value in emit.call_args.kwargs["metrics"].items()}
        row = sanitize({"timestamp": 1234, "eventId": "id", "logStreamName": "stream"}, produced)
        assert row[f"{stage}Active"] == 0
        assert row[f"{stage}QueueDepth"] == 0
        assert row[f"{stage}QueueRejected"] == 0


@pytest.mark.asyncio
async def test_observer_retains_deferred_memory_recovery_evidence():
    guard = MemoryGuard(lambda: MemorySample(100, 100, 0), AsyncMock(), can_reclaim=lambda: True)
    guard.request_reclaim()
    with patch("scholight.web_extract.memory.emit_emf") as emit:
        await guard.tick()
    produced = {
        key: value[0]
        for call in emit.call_args_list
        for key, value in call.kwargs["metrics"].items()
    }
    row = sanitize({"timestamp": 1234, "eventId": "id", "logStreamName": "stream"}, produced)
    assert row["MemoryIdleReclaim"] == 1


@pytest.mark.asyncio
async def test_observer_retains_worker_oom_count_after_resident_memory_recovers():
    guard = MemoryGuard(lambda: MemorySample(100, 100, 0, oom_kills=2), AsyncMock())
    with patch("scholight.web_extract.memory.emit_emf") as emit:
        await guard.tick()
    produced = {key: value[0] for key, value in emit.call_args.kwargs["metrics"].items()}
    row = sanitize({"timestamp": 1234, "eventId": "id", "logStreamName": "stream"}, produced)
    assert row["MemoryOOMKills"] == 2
