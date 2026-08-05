from scholight.survey.metrics import is_provider_throttled


def test_provider_throttle_classifier_accepts_sanitized_rate_limit_codes() -> None:
    assert is_provider_throttled("model_rate_limited") is True
    assert is_provider_throttled("survey_provider_rate_limited") is True
    assert is_provider_throttled("provider_throttled") is True


def test_provider_throttle_classifier_rejects_other_or_missing_errors() -> None:
    assert is_provider_throttled("survey_timed_out") is False
    assert is_provider_throttled(None) is False
