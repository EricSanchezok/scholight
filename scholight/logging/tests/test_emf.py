"""CloudWatch EMF contract tests."""

from __future__ import annotations

from unittest.mock import patch

from scholight.logging.emf import emit_emf


def test_emf_uses_only_low_cardinality_dimensions() -> None:
    with patch("scholight.logging.emf.logger.info") as info:
        emit_emf(
            service="api",
            strength="standard",
            transport="rest",
            outcome="success",
            metrics={"RequestCount": (1, "Count"), "SuccessLatency": (12.5, "Milliseconds")},
        )

    payload = info.call_args.kwargs
    metric_block = payload["_aws"]["CloudWatchMetrics"][0]
    assert metric_block["Namespace"] == "Scholight/Production"
    assert metric_block["Dimensions"] == [["service", "strength", "transport", "outcome"]]
    assert "request_id" not in payload
    assert "query" not in payload


def test_emf_logging_failure_never_breaks_application_work() -> None:
    with patch("scholight.logging.emf.logger.info", side_effect=RuntimeError("logging down")):
        emit_emf(service="api", metrics={"RequestCount": (1, "Count")})
