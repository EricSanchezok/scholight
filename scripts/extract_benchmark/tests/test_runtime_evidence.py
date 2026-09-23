from __future__ import annotations

import json

from runtime_probes import queue_evidence


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
