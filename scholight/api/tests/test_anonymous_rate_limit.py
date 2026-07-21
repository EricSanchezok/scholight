"""Anonymous search minute-limit integration tests."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from scholight.api.search_access import anonymous_search_limiter


@pytest.mark.asyncio
async def test_app_registers_complete_slowapi_stack(api_app: FastAPI) -> None:
    assert api_app.state.limiter is anonymous_search_limiter
    assert RateLimitExceeded in api_app.exception_handlers
    assert any(middleware.cls is SlowAPIMiddleware for middleware in api_app.user_middleware)


@pytest.mark.asyncio
async def test_anonymous_validation_errors_consume_minute_attempts(
    api_app: FastAPI,
) -> None:
    transport = httpx.ASGITransport(app=api_app, client=("192.0.2.11", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [await client.post("/search", json={}) for _ in range(31)]

    assert [response.status_code for response in responses[:30]] == [422] * 30
    assert responses[30].status_code == 429
    assert responses[30].json() == {
        "detail": {
            "code": "anonymous_rate_limit_exceeded",
            "message": "Anonymous search rate limit exceeded.",
            "retryable": True,
        }
    }
    assert int(responses[30].headers["retry-after"]) >= 1
    assert "x-request-id" in responses[30].headers


@pytest.mark.asyncio
async def test_authorization_header_bypasses_anonymous_minute_bucket(
    api_app: FastAPI,
) -> None:
    transport = httpx.ASGITransport(app=api_app, client=("192.0.2.12", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [
            await client.post(
                "/search",
                json={},
                headers={"Authorization": "Bearer invalid"},
            )
            for _ in range(31)
        ]

    assert 429 not in {response.status_code for response in responses}


@pytest.mark.asyncio
async def test_non_search_routes_are_not_anonymously_limited(api_app: FastAPI) -> None:
    transport = httpx.ASGITransport(app=api_app, client=("192.0.2.13", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = [await client.get("/livez") for _ in range(31)]

    assert {response.status_code for response in responses} == {200}
