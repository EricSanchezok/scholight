"""Regression tests for the deterministic Survey E2E model."""

from tests.survey_e2e.fake_model import _response, _stage, _tool_call


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
