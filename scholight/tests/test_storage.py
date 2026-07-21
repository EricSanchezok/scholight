"""Cross-process scheduler generation lock tests."""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path
from typing import Protocol

import pytest

from scholight.config import settings
from scholight.storage import Storage


class _SettableEvent(Protocol):
    def set(self) -> None: ...


def _hold_generation_lock(data_root: str, arxiv_id: str, ready: _SettableEvent) -> None:
    settings.data_root = data_root
    storage = Storage()
    with storage.generation_lock(arxiv_id):
        ready.set()
        time.sleep(0.25)


def test_generation_lock_serializes_same_paper_across_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    storage = Storage()
    ready = multiprocessing.Event()
    process = multiprocessing.Process(
        target=_hold_generation_lock,
        args=(str(tmp_path), "2401.00001", ready),
    )
    process.start()
    try:
        assert ready.wait(timeout=2)
        started = time.monotonic()
        with storage.generation_lock("2401.00001"):
            elapsed = time.monotonic() - started
    finally:
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)

    assert process.exitcode == 0
    assert elapsed >= 0.15


def test_generation_locks_do_not_serialize_different_papers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    storage = Storage()
    ready = multiprocessing.Event()
    process = multiprocessing.Process(
        target=_hold_generation_lock,
        args=(str(tmp_path), "2401.00001", ready),
    )
    process.start()
    try:
        assert ready.wait(timeout=2)
        started = time.monotonic()
        with storage.generation_lock("2401.00002"):
            elapsed = time.monotonic() - started
    finally:
        process.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)

    assert process.exitcode == 0
    assert elapsed < 0.1
