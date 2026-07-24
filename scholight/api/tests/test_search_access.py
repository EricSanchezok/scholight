"""Anonymous IP privacy and API runtime configuration tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scholight.api.search_access import anonymous_ip_digest
from scholight.config import Settings, settings, validate_api_runtime_settings


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
