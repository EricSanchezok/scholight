"""Best-effort Survey navigation title tests."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from scholight.config import settings
from scholight.survey import title as title_module

pytestmark = pytest.mark.asyncio


async def test_generate_survey_title_normalizes_model_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "# \u201c思维链压缩的推理与训练策略\u201d\n"}}]
            },
            request=request,
        )
    )
    async with httpx.AsyncClient(transport=transport) as client:
        title = await title_module.generate_survey_title("很长的调研需求", client=client)

    assert title == "思维链压缩的推理与训练策略"


async def test_generate_survey_title_falls_back_on_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    transport = httpx.MockTransport(
        lambda request: httpx.Response(503, json={"error": "busy"}, request=request)
    )
    async with httpx.AsyncClient(transport=transport) as client:
        title = await title_module.generate_survey_title("retrieval research", client=client)

    assert title is None


async def test_generate_survey_title_skips_call_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "")

    title = await title_module.generate_survey_title("retrieval research")

    assert title is None


async def test_generate_survey_title_calls_provider_for_short_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured["content"] = payload["messages"][1]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "RAG"}}]},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await title_module.generate_survey_title("RAG", client=client)

    assert captured["content"] == "RAG"


async def test_generate_survey_title_has_total_time_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    monkeypatch.setattr(title_module, "_REQUEST_TIMEOUT_SECONDS", 0.01)

    async def respond(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Late title"}}]},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        title = await title_module.generate_survey_title("RAG", client=client)

    assert title is None
