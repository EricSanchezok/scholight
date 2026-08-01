from __future__ import annotations

import pytest

from scholight.config import settings, validate_extract_runtime_settings


def test_extract_runtime_requires_independent_internal_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "extract_internal_token", "short")

    with pytest.raises(ValueError, match="SCHOLIGHT_EXTRACT_INTERNAL_TOKEN"):
        validate_extract_runtime_settings()


def test_extract_runtime_accepts_internal_http_service_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "extract_internal_token", "e" * 32)
    monkeypatch.setattr(settings, "extract_service_url", "http://extract:8001")

    validate_extract_runtime_settings()
