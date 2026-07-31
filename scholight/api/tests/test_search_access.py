"""Anonymous IP privacy and API runtime configuration tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scholight.api.search_access import anonymous_ip_digest
from scholight.config import (
    Settings,
    settings,
    validate_api_runtime_settings,
    validate_survey_draft_worker_settings,
)


def test_anonymous_ip_digest_is_deterministic_and_contains_no_plain_ip() -> None:
    digest = anonymous_ip_digest("192.0.2.10", "s" * 32)

    assert digest == anonymous_ip_digest("192.0.2.10", "s" * 32)
    assert len(digest) == 32
    assert b"192.0.2.10" not in digest


def test_anonymous_ip_digest_is_keyed() -> None:
    assert anonymous_ip_digest("192.0.2.10", "a" * 32) != anonymous_ip_digest(
        "192.0.2.10", "b" * 32
    )


def test_anonymous_ip_digest_normalizes_ipv6_and_mapped_ipv4() -> None:
    assert anonymous_ip_digest("2001:db8::1", "s" * 32) == anonymous_ip_digest(
        "2001:0db8:0:0:0:0:0:1", "s" * 32
    )
    assert anonymous_ip_digest("::ffff:192.0.2.10", "s" * 32) == anonymous_ip_digest(
        "192.0.2.10", "s" * 32
    )


@pytest.mark.parametrize(
    "field",
    [
        "anonymous_rate_limit_per_minute",
        "anonymous_standard_daily_limit",
        "anonymous_thorough_daily_limit",
    ],
)
def test_anonymous_limits_must_be_positive(field: str) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({field: 0})


def test_generic_settings_allow_missing_api_hmac_secret() -> None:
    loaded = Settings(_env_file=None)  # type: ignore[call-arg]

    assert loaded.anonymous_quota_hmac_secret == ""


def test_server_concurrency_limit_defaults_to_last_resort_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCHOLIGHT_SERVER_LIMIT_CONCURRENCY", raising=False)

    loaded = Settings(_env_file=None)  # type: ignore[call-arg]

    assert loaded.server_limit_concurrency == 96


def test_server_concurrency_limit_can_be_enabled_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCHOLIGHT_SERVER_LIMIT_CONCURRENCY", "256")

    loaded = Settings(_env_file=None)  # type: ignore[call-arg]

    assert loaded.server_limit_concurrency == 256


def test_api_runtime_requires_hmac_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "j" * 32)
    monkeypatch.setattr(settings, "anonymous_quota_hmac_secret", "short")
    monkeypatch.setattr(settings, "access_key_hmac_secret", "k" * 32)
    monkeypatch.setattr(settings, "mcp_delegation_jwt_secret", "d" * 32)
    monkeypatch.setattr(settings, "proxy_headers", False)
    monkeypatch.setattr(settings, "cors_allow_origins", ["http://localhost:3000"])

    with pytest.raises(ValueError, match="ANONYMOUS_QUOTA_HMAC_SECRET"):
        validate_api_runtime_settings()


def test_api_runtime_requires_access_key_hmac_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "j" * 32)
    monkeypatch.setattr(settings, "anonymous_quota_hmac_secret", "h" * 32)
    monkeypatch.setattr(settings, "access_key_hmac_secret", "short")
    monkeypatch.setattr(settings, "mcp_delegation_jwt_secret", "d" * 32)
    monkeypatch.setattr(settings, "proxy_headers", False)
    monkeypatch.setattr(settings, "cors_allow_origins", ["http://localhost:3000"])

    with pytest.raises(ValueError, match="ACCESS_KEY_HMAC_SECRET"):
        validate_api_runtime_settings()


def test_api_runtime_rejects_untrusted_proxy_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "j" * 32)
    monkeypatch.setattr(settings, "anonymous_quota_hmac_secret", "h" * 32)
    monkeypatch.setattr(settings, "access_key_hmac_secret", "k" * 32)
    monkeypatch.setattr(settings, "mcp_delegation_jwt_secret", "d" * 32)
    monkeypatch.setattr(settings, "proxy_headers", True)
    monkeypatch.setattr(settings, "forwarded_allow_ips", "*")
    monkeypatch.setattr(settings, "cors_allow_origins", ["http://localhost:3000"])

    with pytest.raises(ValueError, match="FORWARDED_ALLOW_IPS"):
        validate_api_runtime_settings()


def test_api_runtime_rejects_wildcard_cors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "j" * 32)
    monkeypatch.setattr(settings, "anonymous_quota_hmac_secret", "h" * 32)
    monkeypatch.setattr(settings, "access_key_hmac_secret", "k" * 32)
    monkeypatch.setattr(settings, "mcp_delegation_jwt_secret", "d" * 32)
    monkeypatch.setattr(settings, "proxy_headers", False)
    monkeypatch.setattr(settings, "cors_allow_origins", ["*"])

    with pytest.raises(ValueError, match="CORS_ALLOW_ORIGINS"):
        validate_api_runtime_settings()


def test_api_runtime_requires_delegation_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "j" * 32)
    monkeypatch.setattr(settings, "anonymous_quota_hmac_secret", "h" * 32)
    monkeypatch.setattr(settings, "access_key_hmac_secret", "k" * 32)
    monkeypatch.setattr(settings, "mcp_delegation_jwt_secret", "short")
    monkeypatch.setattr(settings, "proxy_headers", False)
    monkeypatch.setattr(settings, "cors_allow_origins", ["http://localhost:3000"])

    with pytest.raises(ValueError, match="MCP_DELEGATION_JWT_SECRET"):
        validate_api_runtime_settings()


@pytest.mark.parametrize(
    ("field", "message"),
    [
        ("zilliz_uri", "ZILLIZ_URI"),
        ("zilliz_token", "ZILLIZ_TOKEN"),
        ("embedding_base_url", "EMBEDDING_BASE_URL"),
    ],
)
def test_api_runtime_requires_search_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    message: str,
) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "j" * 32)
    monkeypatch.setattr(settings, "anonymous_quota_hmac_secret", "h" * 32)
    monkeypatch.setattr(settings, "access_key_hmac_secret", "k" * 32)
    monkeypatch.setattr(settings, "mcp_delegation_jwt_secret", "d" * 32)
    monkeypatch.setattr(settings, "zilliz_uri", "https://zilliz.example.invalid")
    monkeypatch.setattr(settings, "zilliz_token", "fixture-token")
    monkeypatch.setattr(settings, "embedding_base_url", "https://embedding.example.invalid/v1")
    monkeypatch.setattr(settings, "cors_allow_origins", ["http://localhost:3000"])
    monkeypatch.setattr(settings, field, "")

    with pytest.raises(ValueError, match=message):
        validate_api_runtime_settings()


def test_survey_provider_keys_use_unprefixed_environment_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-secret")
    monkeypatch.setenv("IMAGE_GEN_API_KEY", "image-secret")
    monkeypatch.delenv("SCHOLIGHT_DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("SCHOLIGHT_IMAGE_GEN_API_KEY", raising=False)

    loaded = Settings(_env_file=None)  # type: ignore[call-arg]

    assert loaded.deepseek_api_key == "deepseek-secret"
    assert loaded.image_gen_api_key == "image-secret"


def test_runtime_validation_requires_survey_boundaries_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "j" * 32)
    monkeypatch.setattr(settings, "anonymous_quota_hmac_secret", "h" * 32)
    monkeypatch.setattr(settings, "access_key_hmac_secret", "k" * 32)
    monkeypatch.setattr(settings, "mcp_delegation_jwt_secret", "d" * 32)
    monkeypatch.setattr(settings, "zilliz_uri", "https://zilliz.example.invalid")
    monkeypatch.setattr(settings, "zilliz_token", "fixture-token")
    monkeypatch.setattr(settings, "embedding_base_url", "https://embedding.example.invalid/v1")
    monkeypatch.setattr(settings, "cors_allow_origins", ["http://localhost:3000"])
    monkeypatch.setattr(settings, "survey_enabled", True)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "")
    monkeypatch.setattr(settings, "survey_s3_bucket", "")

    with pytest.raises(ValueError, match="SCHOLIGHT_SURVEY_MCP_JWT_SECRET"):
        validate_api_runtime_settings()


def test_draft_worker_does_not_require_artifact_or_image_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "survey_enabled", True)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "deepseek_api_key", "deepseek-secret")
    monkeypatch.setattr(settings, "survey_s3_bucket", "")
    monkeypatch.setattr(settings, "image_gen_api_key", "")

    validate_survey_draft_worker_settings()
