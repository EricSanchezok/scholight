"""Best-effort Survey navigation title tests."""

from __future__ import annotations

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
            json={"choices": [{"message": {"content": "思维链压缩的推理与训练策略"}}]},
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


async def test_generate_survey_title_prompt_requires_direct_bounded_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deepseek_api_key", "test-key")
    captured: dict[str, object] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        captured["system"] = payload["messages"][0]["content"]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "Reasoning compression strategies"}}]},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await title_module.generate_survey_title("RAG", client=client)

    assert "Output exactly the title text and nothing else" in str(captured["system"])


async def test_normalize_title_preserves_the_complete_response() -> None:
    assert title_module._normalize_title("Reasoning compression\nacross training") == (
        "Reasoning compression across training"
    )


async def test_normalize_title_rejects_only_storage_overflow() -> None:
    assert title_module._normalize_title("x" * 161) is None
