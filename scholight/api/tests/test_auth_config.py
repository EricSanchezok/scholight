"""API authentication configuration startup tests."""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from scholight.api.app import create_app
from scholight.config import Settings, settings

pytestmark = pytest.mark.filterwarnings(
    "ignore:'asyncio.iscoroutinefunction' is deprecated.*:DeprecationWarning"
)


@pytest.fixture(autouse=True)
def valid_non_jwt_api_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "anonymous_quota_hmac_secret", "h" * 32)
    monkeypatch.setattr(settings, "access_key_hmac_secret", "k" * 32)
    monkeypatch.setattr(settings, "proxy_headers", False)
    monkeypatch.setattr(settings, "cors_allow_origins", ["http://localhost:3000"])


def test_create_app_rejects_missing_jwt_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SCHOLIGHT_AUTH_JWT_SECRET", raising=False)
    fresh_settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert fresh_settings.auth_jwt_secret == ""

    monkeypatch.setattr(settings, "auth_jwt_secret", fresh_settings.auth_jwt_secret)
    monkeypatch.setattr(settings, "jwt_secret", fresh_settings.jwt_secret)
    with pytest.raises(ValueError, match="at least 32 UTF-8 bytes"):
        create_app()


def test_create_app_rejects_blank_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "   ")

    with pytest.raises(ValueError, match="at least 32 UTF-8 bytes"):
        create_app()


def test_create_app_rejects_short_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "a" * 31)

    with pytest.raises(ValueError, match="at least 32 UTF-8 bytes"):
        create_app()


def test_create_app_accepts_jwt_secret_at_minimum_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "a" * 32)

    assert isinstance(create_app(), FastAPI)


def test_browser_auth_contract_uses_cookie_refresh_without_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "jwt_secret", "a" * 32)
    schema = create_app().openapi()

    login_schema = schema["paths"]["/auth/login"]["post"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]
    refresh = schema["paths"]["/auth/refresh"]["post"]

    assert login_schema["$ref"].endswith("/AccessTokenResponse")
    assert refresh["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/AccessTokenResponse"
    )
    assert "requestBody" not in refresh
