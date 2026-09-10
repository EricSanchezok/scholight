"""Retained full-pipeline regression profile; lean tests override it explicitly."""

import pytest

from scholight.config import settings


@pytest.fixture(autouse=True)
def full_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "runtime_profile", "full")
