"""Tiny CloudWatch Embedded Metric Format emitter with bounded dimensions."""

from __future__ import annotations

import os
import time
from collections.abc import Mapping
from typing import Literal

import structlog

logger = structlog.get_logger("scholight.metrics")
MetricUnit = Literal[
    "Count",
    "Milliseconds",
    "Seconds",
    "Percent",
    "Bytes",
    "None",
]
_ALLOWED_DIMENSIONS = frozenset({"service", "strength", "transport", "outcome"})
# Search already emits separate detail and service-aggregate events. Extract only
# the existing aggregate for these names; merging the detail would double count.
_SEARCH_AGGREGATE_METRICS = frozenset({"RequestCount", "InFlight", "Unexpected5xx"})
_EXTRACT_END_TO_END_METRICS = frozenset({"RequestCount", "Latency"})
_CRITICAL_METRICS = frozenset(
    {
        # Dependency failures and durable accounting/history backpressure.
        "SearchTimeout",
        "SearchConnectionReset",
        "SearchConnectError",
        "SearchDependencyFailure",
        "SuccessLatency",
        "ErrorLatency",
        "HistoryQueueDepth",
        "HistoryQueueDropped",
        "UsageQueueDepth",
        "UsageQueueDropped",
        "ExtractServiceFailure",
        # Keep capacity and safety observations; the local guards run unchanged.
        "MemoryWorkingSet",
        "MemoryOOMKills",
        "MemoryAdmissionPaused",
        "MemorySampleFailure",
        "MemoryReclaimFailure",
        "MemoryReservationRejected",
        "DownloadQueueDepth",
        "DownloadQueueRejected",
        "ParseQueueDepth",
        "ParseQueueRejected",
        "BrowserQueueDepth",
        "BrowserQueueRejected",
        # Preserve recovery visibility independently of successful/idle batches.
        "IngestionBacklog",
        "IngestionDeadTotal",
        "IngestionOldestAge",
        "IngestionDead",
        "IngestionRetry",
        "IngestionLeaseLost",
        "IngestionDrainJobs",
        "IngestionSucceeded",
        # Retain dormant Survey protection and failure signals for future use.
        "SurveyDraftOutstanding",
        "SurveyDraftRunning",
        "SurveyDraftQueued",
        "SurveyDraftOldestQueuedAge",
        "SurveyDraftUsersAtConcurrencyLimit",
        "SurveyJobOutstanding",
        "SurveyJobRunning",
        "SurveyJobQueued",
        "SurveyJobOldestQueuedAge",
        "SurveyJobUsersAtConcurrencyLimit",
        "SurveyTaskProtectionFailure",
        "SurveyEmailNotificationPending",
        "SurveyEmailNotificationDead",
        "SurveyEmailNotificationOldestAge",
        "SurveyRuntimeFailure",
        "SurveyFullTextRuntimeFailure",
        "SurveyFinalizationFailure",
        "SurveyModelTerminalFailure",
        "SurveyModelCompletionFailure",
        "SurveyContractAnomaly",
        "SurveyProviderThrottled",
        "SurveyProviderRetry",
        "SurveyToolFailure",
        "SurveyDiagnosticsWriteFailure",
        "SurveyLastActivityAge",
        "SurveyModelCanaryCount",
        "SurveyFullTextCanaryCount",
        "SurveyImageCanaryCount",
        "SurveyImageGenerationCount",
        "SurveyChartRejectedCount",
        "SurveyFullTextCoverage",
    }
)


def emit_emf(
    *,
    service: str,
    metrics: Mapping[str, tuple[float | int, MetricUnit]],
    strength: str | None = None,
    transport: str | None = None,
    outcome: str | None = None,
) -> None:
    """Log every field, extracting only the configured bounded CloudWatch metrics."""
    dimensions = {
        "service": service,
        **({"strength": strength} if strength is not None else {}),
        **({"transport": transport} if transport is not None else {}),
        **({"outcome": outcome} if outcome is not None else {}),
    }
    if not set(dimensions).issubset(_ALLOWED_DIMENSIONS):
        raise ValueError("unsupported EMF dimension")
    # Keep this low-level helper independent of Settings/.env initialization,
    # matching logging's renderer configuration. Only this non-secret key is read.
    extract_all = os.environ.get("SCHOLIGHT_CLOUDWATCH_EMF_MODE", "critical") == "all"
    definitions = [
        {"Name": name, "Unit": unit}
        for name, (_, unit) in metrics.items()
        if extract_all
        or name in _CRITICAL_METRICS
        or (service == "api" and len(dimensions) == 1 and name in _SEARCH_AGGREGATE_METRICS)
        or (service in {"extract", "extract-api"} and name in _EXTRACT_END_TO_END_METRICS)
    ]
    metadata: dict[str, object] = {}
    if definitions:
        metadata["_aws"] = {
            "Timestamp": time.time_ns() // 1_000_000,
            "CloudWatchMetrics": [
                {
                    "Namespace": "Scholight/Production",
                    "Dimensions": [list(dimensions)],
                    "Metrics": definitions,
                }
            ],
        }
    try:
        logger.info(
            "emf_metric",
            **dimensions,
            **{name: value for name, (value, _) in metrics.items()},
            **metadata,
        )
    except Exception:
        # Metrics are best effort. Recursively logging this failure would risk the
        # same broken handler, so it must never affect search or ingestion work.
        return


__all__ = ["MetricUnit", "emit_emf"]
