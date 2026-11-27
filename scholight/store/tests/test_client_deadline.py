"""A destination-verification deadline also bounds singleton connection waits."""

from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import pytest

from scholight.store import client


def test_expired_connection_deadline_never_constructs_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = Mock(side_effect=AssertionError("connection after deadline"))
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "PyMilvusClient", factory)

    with pytest.raises(TimeoutError):
        client.get_client(timeout=5.0, deadline=time.monotonic() - 1)

    factory.assert_not_called()


def test_connection_lock_wait_obeys_verification_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock = threading.RLock()
    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with lock:
            held.set()
            release.wait(0.5)

    thread = threading.Thread(target=hold)
    thread.start()
    factory = Mock(side_effect=AssertionError("connection while another thread owns lock"))
    monkeypatch.setattr(client, "_client", None)
    monkeypatch.setattr(client, "_lock", lock)
    monkeypatch.setattr(client, "PyMilvusClient", factory)
    try:
        assert held.wait(0.5)
        with pytest.raises(TimeoutError):
            client.get_client(timeout=5.0, deadline=time.monotonic() + 0.01)
        factory.assert_not_called()
    finally:
        release.set()
        thread.join(0.5)
    assert not thread.is_alive()
