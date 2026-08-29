from unittest.mock import patch

from scholight.survey.metrics import emit_chart_metrics, is_provider_throttled


def test_provider_throttle_classifier_accepts_sanitized_rate_limit_codes() -> None:
    assert is_provider_throttled("model_rate_limited") is True
    assert is_provider_throttled("survey_provider_rate_limited") is True
    assert is_provider_throttled("provider_throttled") is True


def test_provider_throttle_classifier_rejects_other_or_missing_errors() -> None:
    assert is_provider_throttled("survey_timed_out") is False
    assert is_provider_throttled(None) is False


def test_chart_metrics_emit_bounded_render_counters() -> None:
    with patch("scholight.survey.metrics.emit_emf") as emit:
        emit_chart_metrics(chart_count=3, chart_rejected_count=1)

    emit.assert_called_once_with(
        service="survey-full-worker",
        metrics={
            "SurveyChartRenderedCount": (3, "Count"),
            "SurveyChartRejectedCount": (1, "Count"),
        },
    )
