"""Regression tests for the deterministic Survey E2E model."""

import json

import httpx
import pytest

from scholight.api.models.search import PublicSearchRequest, SearchStrength
from tests.survey_e2e.fake_model import _response, _stage, _tool_call, app


@pytest.mark.asyncio
async def test_reference_expansion_uses_the_current_public_search_contract() -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/chat/completions",
            json={
                "messages": [
                    {"role": "system", "content": "generates a list of expanded references"}
                ],
                "tools": [{"type": "function", "function": {"name": "scholight__search_papers"}}],
            },
        )
    call = response.json()["choices"][0]["message"]["tool_calls"][0]
    request = PublicSearchRequest.model_validate(json.loads(call["function"]["arguments"]))
    assert request.strength is SearchStrength.STANDARD


def test_tool_call_response_uses_deepseek_string_content() -> None:
    message = _tool_call("fs", {"action": "write"})

    assert message["content"] == ""
    assert message["reasoning_content"] == "synthetic tool reasoning"
    assert message["tool_calls"][0]["index"] == 0


def test_response_includes_deepseek_native_required_fields() -> None:
    response = _response(_tool_call("fs", {"action": "write"}), finish_reason="tool_calls")

    assert response["choices"][0]["logprobs"] is None
    assert response["usage"]["prompt_cache_hit_tokens"] == 0
    assert response["usage"]["prompt_cache_miss_tokens"] == 0


def test_stage_uses_current_system_prompt_instead_of_historical_tool_calls() -> None:
    body = {
        "messages": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "You are the SurveyOutline author for the final report.",
                    }
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {
                            "name": "spawn_SectionExpander",
                            "arguments": "{}",
                        },
                    }
                ],
            },
        ]
    }

    assert _stage(body) == "survey_outline"
