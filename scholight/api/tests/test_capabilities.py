"""Public product capability contract tests."""

import httpx
import pytest
from fastapi import FastAPI

from scholight.config import settings

pytestmark = pytest.mark.asyncio


async def test_capabilities_fail_closed_without_authentication(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "off")

    response = await api_client.get("/capabilities")

    assert response.status_code == 200
    assert response.json() == {"survey": "off"}


async def test_capabilities_can_publish_survey(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "survey_runtime_enabled", True)
    monkeypatch.setattr(settings, "survey_public_mode", "all")

    response = await api_client.get("/capabilities")

    assert response.status_code == 200
    assert response.json() == {"survey": "all"}
