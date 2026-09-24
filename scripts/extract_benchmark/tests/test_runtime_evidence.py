from __future__ import annotations

import json

import pytest
from runtime_probes import queue_evidence


@pytest.mark.parametrize("stage,limit", [("Download", 2000), ("Parse", 2000), ("Browser", 5000)])
def test_queue_gate_rejects_wait_overrun_even_when_total_latency_is_short(stage, limit):
    rows = [
        {f"{name}Active": 0, f"{name}QueueDepth": 0} for name in ("Download", "Parse", "Browser")
    ]
    rows.append({"phases_ms": {f"{stage}QueueLatency": limit + 100}})
    assert not queue_evidence("\n".join(json.dumps(row) for row in rows))["passed"]


def test_queue_gate_reports_scheduler_overshoot_without_including_execution():
    rows = [
        {f"{stage}Active": 0, f"{stage}QueueDepth": 0} for stage in ("Download", "Parse", "Browser")
    ]
    rows.append({"phases_ms": {"ParseQueueLatency": 2004, "ParseLatency": 3500}})
    report = queue_evidence("\n".join(json.dumps(row) for row in rows))
    assert report["passed"]
    assert report["stages"]["Parse"]["max_wait_ms"] == 2004
    assert report["stages"]["Parse"]["max_wait_overshoot_ms"] == 4


def test_overload_uses_the_callers_total_budget_and_retains_the_old_screen():
    from runtime_probes import overload_evidence

    report = overload_evidence([{"status": 200, "seconds": 4.3}, {"status": 503, "seconds": 2}], 60)
    assert report["passed"] and not report["legacy_3_2_second_screen_passed"]
    assert report["request_budget_seconds"] == 8
    assert not overload_evidence(
        [{"status": 200, "seconds": 8.1}, {"status": 503, "seconds": 2}], 60
    )["passed"]
    assert not overload_evidence(
        [{"status": 500, "seconds": 1}, {"status": 503, "seconds": 2}], 60
    )["passed"]


def test_missing_or_excess_queue_metrics_cannot_pass_runtime_gate():
    assert not queue_evidence("")["passed"]
    rows = [
        json.dumps({f"{stage}Active": 0, f"{stage}QueueDepth": 0})
        for stage in ("Download", "Parse", "Browser")
    ]
    rows.insert(0, json.dumps({"DownloadActive": 2, "DownloadQueueDepth": 9}))
    assert not queue_evidence("\n".join(rows))["passed"]


def test_queue_evidence_requires_all_execution_and_waiters_to_leave():
    rows = [
        json.dumps({f"{stage}Active": 1, f"{stage}QueueDepth": 0})
        for stage in ("Download", "Parse", "Browser")
    ]
    assert not queue_evidence("\n".join(rows))["passed"]
