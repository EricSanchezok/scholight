"""API health endpoint tests without real PostgreSQL or Zilliz connections."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

from scholight.api import app as app_module
from scholight.api.app import create_app
from scholight.config import settings

pytestmark = pytest.mark.filterwarnings(
    "ignore:'asyncio.iscoroutinefunction' is deprecated.*:DeprecationWarning"
)


@pytest.fixture(autouse=True)
def reset_readiness_cache() -> None:
    app_module._reset_dependency_probe_cache()
    settings.anonymous_quota_hmac_secret = "h" * 32
    settings.access_key_hmac_secret = "k" * 32
    settings.proxy_headers = False
    settings.cors_allow_origins = ["http://localhost:3000"]


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setattr(settings, "jwt_secret", "test-jwt-secret-with-at-least-32-bytes")
    return create_app()


@pytest.mark.asyncio
async def test_livez_reports_process_alive(app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/livez")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_readyz_reports_dependencies_ready(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection = MagicMock()
    connection.execute = AsyncMock(return_value="SELECT 1")
    acquire = AsyncMock()
    acquire.__aenter__.return_value = connection
    pool = MagicMock()
    pool.acquire.return_value = acquire
    zilliz_client = MagicMock()
    zilliz_client.list_collections.return_value = ["arxiv_papers", "arxiv_chunks"]

    monkeypatch.setattr("scholight.db.client.get_pool", lambda: pool)
    monkeypatch.setattr("scholight.store.client.get_client", lambda: zilliz_client)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/readyz")

    assert response.status_code == 200
    zilliz_client.list_collections.assert_called_once_with(
        timeout=app_module._DEPENDENCY_TIMEOUT_SECONDS
    )


@pytest.mark.asyncio
async def test_concurrent_readyz_requests_share_one_dependency_probe(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    postgres_probe = AsyncMock(return_value=True)
    zilliz_probe = AsyncMock(return_value=True)
    monkeypatch.setattr(app_module, "_probe_postgres", postgres_probe)
    monkeypatch.setattr(app_module, "_probe_zilliz", zilliz_probe)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(*(client.get("/readyz") for _ in range(10)))

    assert {response.status_code for response in responses} == {200}
    postgres_probe.assert_awaited_once_with()
    zilliz_probe.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_readyz_returns_503_without_exception_details(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scholight.db.client.get_pool",
        MagicMock(side_effect=RuntimeError("postgres password leaked")),
    )
    monkeypatch.setattr(
        "scholight.store.client.get_client",
        MagicMock(side_effect=RuntimeError("zilliz token leaked")),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "postgres": "down", "zilliz": "down"}


@pytest.mark.asyncio
async def test_health_remains_backward_compatible(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "scholight.db.client.get_pool",
        MagicMock(side_effect=RuntimeError("postgres unavailable")),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")

    assert response.json() == {"status": "degraded", "pg": "PostgreSQL unreachable"}
