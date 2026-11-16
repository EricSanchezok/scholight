"""CloudWatch EMF contract tests."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from scholight.logging.emf import MetricUnit, emit_emf


@pytest.fixture(autouse=True)
def default_emf_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHOLIGHT_CLOUDWATCH_EMF_MODE", raising=False)


def test_emf_uses_only_low_cardinality_dimensions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHOLIGHT_CLOUDWATCH_EMF_MODE", "all")
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


def test_emf_includes_cloudwatch_epoch_millisecond_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHOLIGHT_CLOUDWATCH_EMF_MODE", "all")
    with (
        patch("scholight.logging.emf.time.time_ns", return_value=1_785_051_843_123_456_789),
        patch("scholight.logging.emf.logger.info") as info,
    ):
        emit_emf(service="api", metrics={"RequestCount": (1, "Count")})

    assert info.call_args.kwargs["_aws"]["Timestamp"] == 1_785_051_843_123


def test_emf_logging_failure_never_breaks_application_work() -> None:
    with patch("scholight.logging.emf.logger.info", side_effect=RuntimeError("logging down")):
        emit_emf(service="api", metrics={"RequestCount": (1, "Count")})


def test_default_policy_preserves_diagnostic_log_fields_without_extracting_metrics() -> None:
    with patch("scholight.logging.emf.logger.info") as info:
        emit_emf(
            service="extract-api",
            transport="mcp",
            outcome="success",
            metrics={"OutputBytes": (12, "Bytes"), "Pagination": (1, "Count")},
        )

    assert info.call_args.kwargs == {
        "service": "extract-api",
        "transport": "mcp",
        "outcome": "success",
        "OutputBytes": 12,
        "Pagination": 1,
    }


def test_mixed_payload_keeps_all_log_values_but_extracts_only_critical_metrics() -> None:
    with patch("scholight.logging.emf.logger.info") as info:
        emit_emf(
            service="extract",
            metrics={"MemoryOOMKills": (1, "Count"), "MemoryAnon": (2048, "Bytes")},
        )

    payload = info.call_args.kwargs
    assert payload["MemoryOOMKills"] == 1
    assert payload["MemoryAnon"] == 2048
    assert payload["_aws"]["CloudWatchMetrics"][0]["Metrics"] == [
        {"Name": "MemoryOOMKills", "Unit": "Count"}
    ]


def test_search_detail_and_aggregate_are_not_extracted_into_duplicate_series() -> None:
    metrics: dict[str, tuple[float | int, MetricUnit]] = {
        "RequestCount": (1, "Count"),
        "Unexpected5xx": (1, "Count"),
    }
    with patch("scholight.logging.emf.logger.info") as info:
        emit_emf(
            service="api", strength="standard", transport="rest", outcome="error", metrics=metrics
        )
        emit_emf(service="api", metrics=metrics)

    detailed, aggregate = [call.kwargs for call in info.call_args_list]
    assert detailed["Unexpected5xx"] == 1
    assert detailed["strength"] == "standard"
    assert "_aws" not in detailed
    assert aggregate["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["service"]]
    assert aggregate["_aws"]["CloudWatchMetrics"][0]["Metrics"] == [
        {"Name": "RequestCount", "Unit": "Count"},
        {"Name": "Unexpected5xx", "Unit": "Count"},
    ]


@pytest.mark.parametrize(
    "name",
    [
        "SearchDependencyFailure",
        "SuccessLatency",
        "UsageQueueDropped",
        "HistoryQueueDropped",
        "MemoryAdmissionPaused",
        "MemoryReservationRejected",
        "ParseQueueRejected",
        "IngestionBacklog",
        "IngestionDeadTotal",
        "IngestionLeaseLost",
        "SurveyTaskProtectionFailure",
        "SurveyEmailNotificationDead",
        "SurveyModelTerminalFailure",
    ],
)
def test_critical_policy_retains_operational_signals_and_their_dimensions(name: str) -> None:
    with patch("scholight.logging.emf.logger.info") as info:
        emit_emf(service="test-service", outcome="failure", metrics={name: (1, "Count")})

    block = info.call_args.kwargs["_aws"]["CloudWatchMetrics"][0]
    assert block["Dimensions"] == [["service", "outcome"]]
    assert block["Metrics"] == [{"Name": name, "Unit": "Count"}]


@pytest.mark.parametrize("service", ["extract", "extract-api"])
def test_extract_keeps_existing_end_to_end_latency_and_request_metrics(service: str) -> None:
    with patch("scholight.logging.emf.logger.info") as info:
        emit_emf(
            service=service,
            outcome="success",
            transport="rest",
            metrics={"Latency": (12.5, "Milliseconds"), "RequestCount": (1, "Count")},
        )

    block = info.call_args.kwargs["_aws"]["CloudWatchMetrics"][0]
    assert block["Dimensions"] == [["service", "transport", "outcome"]]
    assert {d["Name"] for d in block["Metrics"]} == {"Latency", "RequestCount"}


def test_mode_is_configurable_for_rollback_without_enabling_new_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHOLIGHT_CLOUDWATCH_EMF_MODE", "all")
    with patch("scholight.logging.emf.logger.info") as info:
        emit_emf(service="extract", metrics={"MemoryAnon": (2048, "Bytes")})

    assert info.call_args.kwargs["_aws"]["CloudWatchMetrics"][0]["Metrics"] == [
        {"Name": "MemoryAnon", "Unit": "Bytes"}
    ]
