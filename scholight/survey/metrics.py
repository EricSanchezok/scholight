"""Low-cardinality Survey metric classification."""

from __future__ import annotations

from scholight.logging.emf import emit_emf


def is_provider_throttled(error_code: str | None) -> bool:
    """Classify sanitized provider rate-limit codes without exposing provider data."""
    normalized = (error_code or "").lower()
    return "rate_limit" in normalized or "throttl" in normalized


def emit_chart_metrics(*, chart_count: int, chart_rejected_count: int) -> None:
    """Emit bounded chart render counters for one finalized Survey report."""
    emit_emf(
        service="survey-full-worker",
        metrics={
            "SurveyChartRenderedCount": (chart_count, "Count"),
            "SurveyChartRejectedCount": (chart_rejected_count, "Count"),
        },
    )


__all__ = ["emit_chart_metrics", "is_provider_throttled"]
