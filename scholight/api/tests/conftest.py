"""Shared lightweight fixtures for Scholight API contract tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
from cloud_auth.models.user import UserRecord
from fastapi import FastAPI

from scholight.api.app import create_app
from scholight.api.search_access import reset_anonymous_minute_limits
from scholight.config import settings
from scholight.models.search import SearchResult


@pytest.fixture
def active_user() -> UserRecord:
    return UserRecord(
        id=42,
        email="active@example.com",
        password_hash="not-a-real-hash",
        status="active",
        email_verified=True,
    )


@pytest.fixture
def empty_search_result() -> SearchResult:
    return SearchResult(query="test", level=1, total_ms=1.0, hits=[])


@pytest.fixture
def api_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[FastAPI]:
    monkeypatch.setattr(settings, "auth_jwt_secret", "j" * 32)
    monkeypatch.setattr(settings, "jwt_secret", "j" * 32)
    monkeypatch.setattr(settings, "anonymous_quota_hmac_secret", "h" * 32)
    monkeypatch.setattr(settings, "access_key_hmac_secret", "k" * 32)
    monkeypatch.setattr(settings, "mcp_delegation_jwt_secret", "d" * 32)
    monkeypatch.setattr(settings, "survey_mcp_jwt_secret", "s" * 32)
    monkeypatch.setattr(settings, "survey_enabled", False)
    monkeypatch.setattr(settings, "zilliz_uri", "https://zilliz.example.invalid")
    monkeypatch.setattr(settings, "zilliz_token", "fixture-token")
    monkeypatch.setattr(settings, "embedding_base_url", "https://embedding.example.invalid/v1")
    monkeypatch.setattr(settings, "proxy_headers", False)
    monkeypatch.setattr(settings, "forwarded_allow_ips", "127.0.0.1")
    monkeypatch.setattr(settings, "cors_allow_origins", ["http://localhost:3000"])
    reset_anonymous_minute_limits()
    yield create_app()
    reset_anonymous_minute_limits()


@pytest_asyncio.fixture
async def api_client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=api_app, client=("192.0.2.10", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
