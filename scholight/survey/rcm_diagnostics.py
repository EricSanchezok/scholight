"""Strict, content-free projection of RCM completion events."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")
_FAILURE_KINDS = frozenset(
    {
        "authentication",
        "configuration",
        "http_error",
        "invalid_request",
        "network",
        "provider_error",
        "provider_unavailable",
        "rate_limited",
        "timeout",
        "unknown",
    }
)
_REQUEST_CLASSES = frozenset({"request_size", "thinking_tool_history", "unknown_request"})
_COUNT_FIELDS = (
    "serialized_request_bytes",
    "estimated_input_tokens",
    "message_count",
    "tool_definition_count",
    "tool_call_count",
    "tool_result_count",
    "reasoning_content_bytes",
    "unmatched_tool_call_count",
    "duplicate_tool_call_count",
)
_BOOLEAN_FIELDS = ("retryable", "thinking_enabled", "reasoning_content_present")
_MAX_COUNTER = 2**63 - 1


def _safe_identifier(value: object) -> str | None:
    if isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None:
        return value
    return None


def _safe_counter(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_COUNTER:
        return value
    return None


def sanitize_completion_failure(event: Mapping[str, object]) -> dict[str, object]:
    """Return only scalar fields approved for Survey diagnostics."""
    if event.get("type") != "completion_end" or event.get("outcome") != "failure":
        return {}
    result: dict[str, object] = {}
    failure_kind = event.get("failure_kind")
    if isinstance(failure_kind, str) and failure_kind in _FAILURE_KINDS:
        result["failure_kind"] = failure_kind
    status = event.get("http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        result["http_status"] = status
    duration_ms = _safe_counter(event.get("duration_ms"))
    if duration_ms is not None:
        result["duration_ms"] = duration_ms
    for field in _COUNT_FIELDS:
        counter_value = _safe_counter(event.get(field))
        if counter_value is not None:
            result[field] = counter_value
    for field in _BOOLEAN_FIELDS:
        boolean_value = event.get(field)
        if isinstance(boolean_value, bool):
            result[field] = boolean_value
    request_class = event.get("request_class")
    if isinstance(request_class, str) and request_class in _REQUEST_CLASSES:
        result["request_class"] = request_class
    for field in ("provider_code", "provider_type", "request_id"):
        identifier_value = _safe_identifier(event.get(field))
        if identifier_value is not None:
            result[field] = identifier_value
    return result


def terminal_completion_failure(output: str) -> dict[str, object] | None:
    """Return the final completion when it failed; later success clears earlier failure."""
    last_completion: Mapping[str, object] | None = None
    for line in output.splitlines():
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("type") == "completion_end":
            last_completion = candidate
    if last_completion is None or last_completion.get("outcome") != "failure":
        return None
    return sanitize_completion_failure(last_completion)


def completion_failure_semantics(failure: Mapping[str, object]) -> tuple[str, str]:
    """Map sanitized RCM metadata to stable Survey failure semantics."""
    status = failure.get("http_status")
    kind = failure.get("failure_kind")
    if status == 429 or kind == "rate_limited":
        return (
            "survey_provider_rate_limited",
            "A Survey provider is temporarily rate limited.",
        )
    if status in {401, 403} or kind == "authentication":
        return (
            "survey_model_auth_failed",
            "The Survey model could not be authenticated.",
        )
    if (
        status in {408, 425}
        or (isinstance(status, int) and 500 <= status <= 599)
        or kind in {"timeout", "network", "provider_error", "provider_unavailable"}
    ):
        return (
            "survey_provider_unavailable",
            "A Survey provider is temporarily unavailable.",
        )
    if (isinstance(status, int) and 400 <= status <= 499) or kind in {
        "invalid_request",
        "http_error",
    }:
        return (
            "survey_model_request_rejected",
            "The Survey model rejected this research unit.",
        )
    if kind == "configuration":
        return (
            "survey_model_configuration_failed",
            "The Survey model is not configured correctly.",
        )
    return (
        "survey_model_completion_failed",
        "The Survey model did not complete this research unit.",
    )


def attempt_failure_details(failure: Mapping[str, object]) -> dict[str, object]:
    """Translate one sanitized completion event to the compute-attempt allowlist."""
    mapping = {
        "http_status": "provider_status",
        "provider_code": "provider_code",
        "provider_type": "provider_type",
        "request_id": "provider_request_id",
        "request_class": "provider_request_class",
        "serialized_request_bytes": "request_bytes",
        "estimated_input_tokens": "estimated_tokens",
        "message_count": "message_count",
        "tool_definition_count": "tool_count",
        "tool_call_count": "tool_call_count",
        "tool_result_count": "tool_result_count",
        "thinking_enabled": "thinking_enabled",
        "reasoning_content_present": "reasoning_content_present",
        "reasoning_content_bytes": "reasoning_content_length",
        "unmatched_tool_call_count": "unmatched_tool_calls",
        "duplicate_tool_call_count": "duplicate_tool_calls",
    }
    return {target: failure[source] for source, target in mapping.items() if source in failure}


__all__ = [
    "attempt_failure_details",
    "completion_failure_semantics",
    "sanitize_completion_failure",
    "terminal_completion_failure",
]
