"""Survey delegated runtime identity contracts."""

from __future__ import annotations

from uuid import uuid4

import jwt
import pytest

from scholight.config import settings
from scholight.survey.runtime import delegated_authorization, image_canary_environment


def test_survey_delegation_carries_job_correlation_without_provider_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    job_id = uuid4()

    authorization = delegated_authorization(
        user_id=42,
        lifetime_seconds=60,
        survey_job_id=job_id,
    )
    claims = jwt.decode(
        authorization.removeprefix("Bearer "),
        "s" * 32,
        algorithms=["HS256"],
        audience="scholight-mcp",
        issuer="scholight-survey",
    )

    assert claims["survey_job_id"] == str(job_id)
    assert "DEEPSEEK_API_KEY" not in claims


def test_draft_delegation_omits_survey_job_correlation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)

    authorization = delegated_authorization(user_id=42, lifetime_seconds=60)
    claims = jwt.decode(
        authorization.removeprefix("Bearer "),
        "s" * 32,
        algorithms=["HS256"],
        audience="scholight-mcp",
        issuer="scholight-survey",
    )

    assert "survey_job_id" not in claims


def test_image_canary_environment_excludes_model_and_database_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "image_gen_api_key", "image-secret")
    monkeypatch.setattr(settings, "image_gen_api_url", "https://gateway.example/v1/images")
    monkeypatch.setattr(settings, "image_gen_trusted_hosts", "images.example")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "must-not-pass")
    monkeypatch.setenv("SCHOLIGHT_PG_PASSWORD", "must-not-pass")

    environment = image_canary_environment()

    assert environment["IMAGE_GEN_API_KEY"] == "image-secret"
    assert environment["IMAGE_GEN_API_URL"] == "https://gateway.example/v1/images"
    assert environment["IMAGE_GEN_TRUSTED_HOSTS"] == "images.example"
    assert "DEEPSEEK_API_KEY" not in environment
    assert "SCHOLIGHT_PG_PASSWORD" not in environment
