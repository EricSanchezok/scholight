"""Anonymous search minute-limit integration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from scholight.config import settings
from scholight.db.queries_anonymous_quota import AnonymousQuotaReservation
from scholight.models.search import SearchResult


def _reservation() -> AnonymousQuotaReservation:
    return AnonymousQuotaReservation(
        quota_date=datetime(2026, 7, 21, tzinfo=UTC).date(),
        ip_digest=b"d" * 32,
        strength="standard",
        used_count=1,
    )


def test_app_has_no_path_based_rate_limit_middleware(api_app: FastAPI) -> None:
    assert all(
        not middleware.cls.__module__.startswith("slowapi")
        for middleware in api_app.user_middleware
    )


@pytest.mark.asyncio
async def test_only_real_anonymous_searches_consume_minute_attempts(
    api_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "anonymous_rate_limit_per_minute", 2)
    transport = httpx.ASGITransport(app=api_app, client=("192.0.2.11", 12345))
    result = SearchResult(query="retrieval", level=1, total_ms=1.0, hits=[])
    with (
        patch(
            "scholight.api.search_access.reserve_anonymous_daily_quota",
            new_callable=AsyncMock,
            return_value=_reservation(),
        ),
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            return_value=result,
        ),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            invalid = [await client.post("/search", json={}) for _ in range(3)]
            allowed = [await client.post("/search", json={"query": "retrieval"}) for _ in range(2)]
            limited = await client.post("/search", json={"query": "retrieval"})

    assert [response.status_code for response in invalid] == [422] * 3
    assert [response.status_code for response in allowed] == [200, 200]
    assert limited.status_code == 429
    assert limited.json() == {
        "detail": {
            "code": "anonymous_rate_limit_exceeded",
            "message": "Anonymous search rate limit exceeded.",
            "retryable": True,
        }
    }
    assert int(limited.headers["retry-after"]) >= 1
    assert "x-request-id" in limited.headers


@pytest.mark.asyncio
async def test_failed_authorization_does_not_consume_anonymous_minute_bucket(
    api_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "anonymous_rate_limit_per_minute", 1)
    transport = httpx.ASGITransport(app=api_app, client=("192.0.2.12", 12345))
    result = SearchResult(query="retrieval", level=1, total_ms=1.0, hits=[])
    with (
        patch(
            "scholight.api.search_access.reserve_anonymous_daily_quota",
            new_callable=AsyncMock,
            return_value=_reservation(),
        ),
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            return_value=result,
        ),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            unauthorized = [
                await client.post(
                    "/search",
                    json={"query": "retrieval"},
                    headers={"Authorization": "Bearer invalid"},
                )
                for _ in range(3)
            ]
            anonymous = await client.post("/search", json={"query": "retrieval"})

    assert [response.status_code for response in unauthorized] == [401] * 3
    assert anonymous.status_code == 200


@pytest.mark.asyncio
async def test_non_search_routes_are_not_anonymously_limited(api_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=api_app, client=("192.0.2.13", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [await client.get("/livez") for _ in range(31)]

    assert {response.status_code for response in responses} == {200}
