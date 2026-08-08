"""Regression tests for the deterministic Survey E2E model."""

from tests.survey_e2e.fake_model import _stage


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
