"""Content-free RCM completion diagnostic contracts."""

from __future__ import annotations

import json

from scholight.survey.rcm_diagnostics import (
    attempt_failure_details,
    completion_failure_semantics,
    terminal_completion_failure,
)


def test_terminal_completion_failure_retains_only_allowlisted_shape_fields() -> None:
    private_body = "PRIVATE PAPER BODY MUST NOT ENTER DIAGNOSTICS"
    event = {
        "type": "completion_end",
        "outcome": "failure",
        "http_status": 400,
        "failure_kind": "invalid_request",
        "retryable": False,
        "duration_ms": 321,
        "request_class": "thinking_tool_history",
        "provider_code": "invalid_request_error",
        "provider_type": "invalid_request_error",
        "request_id": "req-123",
        "serialized_request_bytes": 524_288,
        "estimated_input_tokens": 131_072,
        "message_count": 14,
        "tool_definition_count": 6,
        "tool_call_count": 4,
        "tool_result_count": 3,
        "thinking_enabled": True,
        "reasoning_content_present": True,
        "reasoning_content_bytes": 4096,
        "unmatched_tool_call_count": 1,
        "duplicate_tool_call_count": 0,
        "message": private_body,
        "prompt": private_body,
    }

    failure = terminal_completion_failure(json.dumps(event))

    assert failure is not None
    assert failure["request_class"] == "thinking_tool_history"
    assert failure["serialized_request_bytes"] == 524_288
    assert failure["tool_call_count"] == 4
    assert private_body not in repr(failure)
    assert "message" not in failure
    assert "prompt" not in failure


def test_success_after_a_failed_completion_is_not_a_terminal_failure() -> None:
    output = "\n".join(
        (
            json.dumps(
                {
                    "type": "completion_end",
                    "outcome": "failure",
                    "http_status": 503,
                    "failure_kind": "provider_error",
                }
            ),
            json.dumps({"type": "completion_end", "outcome": "success"}),
        )
    )

    assert terminal_completion_failure(output) is None


def test_invalid_provider_identifiers_and_unknown_fields_are_dropped() -> None:
    output = json.dumps(
        {
            "type": "completion_end",
            "outcome": "failure",
            "http_status": 400,
            "failure_kind": "invalid_request",
            "request_class": "not-a-real-class",
            "provider_code": "bad code with spaces",
            "provider_type": "x" * 129,
            "request_id": "req/unsafe",
            "serialized_request_bytes": -1,
        }
    )

    failure = terminal_completion_failure(output)

    assert failure == {
        "failure_kind": "invalid_request",
        "http_status": 400,
    }


def test_failure_semantics_and_attempt_mapping_use_stable_fields() -> None:
    failure = {
        "failure_kind": "invalid_request",
        "http_status": 400,
        "request_class": "request_size",
        "provider_code": "context_length_exceeded",
        "serialized_request_bytes": 900_000,
        "estimated_input_tokens": 225_000,
        "message_count": 18,
        "tool_definition_count": 7,
        "tool_call_count": 5,
        "tool_result_count": 4,
        "thinking_enabled": True,
        "reasoning_content_present": True,
        "reasoning_content_bytes": 6000,
        "unmatched_tool_call_count": 1,
        "duplicate_tool_call_count": 0,
    }

    code, _message = completion_failure_semantics(failure)
    details = attempt_failure_details(failure)

    assert code == "survey_model_request_rejected"
    assert details == {
        "provider_status": 400,
        "provider_code": "context_length_exceeded",
        "provider_request_class": "request_size",
        "request_bytes": 900_000,
        "estimated_tokens": 225_000,
        "message_count": 18,
        "tool_count": 7,
        "tool_call_count": 5,
        "tool_result_count": 4,
        "thinking_enabled": True,
        "reasoning_content_present": True,
        "reasoning_content_length": 6000,
        "unmatched_tool_calls": 1,
        "duplicate_tool_calls": 0,
    }
