"""Destination identity reads must share the worker's remaining RPC budget."""

from __future__ import annotations

import time
from unittest.mock import Mock

import pytest

from scholight.config import settings
from scholight.db import ingestion


def test_expired_verification_budget_does_not_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    connect = Mock(side_effect=AssertionError("connection after deadline"))
    monkeypatch.setattr(ingestion, "get_client", connect)
    monkeypatch.setattr(time, "monotonic", lambda: 100.0)

    with pytest.raises(TimeoutError):
        ingestion._actual_target(deadline=99.0, rpc_timeout=5.0)

    connect.assert_not_called()


def test_collection_rpc_timeouts_use_remaining_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    requests: list[tuple[str, float]] = []

    def describe(name: str, *, timeout: float) -> dict[str, str]:
        requests.append((name, timeout))
        clock[0] += 1.0
        return {"collection_id": "papers" if name == "arxiv_papers" else "chunks"}

    client = Mock()
    client.describe_collection.side_effect = describe
    connect = Mock(return_value=client)
    monkeypatch.setattr(ingestion, "get_client", connect)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "zilliz_uri", "https://test.invalid")

    actual = ingestion._actual_target(deadline=103.0, rpc_timeout=5.0)

    connect.assert_called_once_with(timeout=3.0, deadline=103.0)
    assert requests == [("arxiv_papers", 3.0), ("arxiv_chunks", 2.0)]
    assert actual.papers_id == "papers"
