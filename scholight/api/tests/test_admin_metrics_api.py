"""Administration operations and analytics API contracts."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from cloud_auth.models.user import UserRecord
from fastapi import FastAPI

from scholight.api.deps import get_current_user, get_scholight_admin
from scholight.db.client import DBError


def _admin() -> UserRecord:
    return UserRecord(
        id=42,
        email="admin@example.com",
        password_hash="hash",
        status="active",
        email_verified=True,
    )


@pytest.mark.asyncio
async def test_operations_endpoint_returns_product_pipeline_state(api_app: FastAPI) -> None:
    api_app.dependency_overrides[get_scholight_admin] = _admin
    metrics = {
        "sync": {
            "last_successful_date": date(2026, 7, 23),
            "last_started_at": datetime(2026, 7, 24, tzinfo=UTC),
            "last_succeeded_at": datetime(2026, 7, 24, tzinfo=UTC),
            "last_error_code": None,
            "last_error_message": None,
        },
        "queue": {
            "pending": 2,
            "running": 1,
            "retry": 3,
            "succeeded": 10,
            "dead": 1,
            "oldest_waiting_at": datetime(2026, 7, 23, tzinfo=UTC),
        },
        "intake": [],
        "recent_issues": [],
    }

    with patch(
        "scholight.api.routes.admin_operations.query_admin_operations",
        AsyncMock(return_value=metrics),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://test"
        ) as client:
            response = await client.get("/admin/operations/overview")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_analytics_endpoint_returns_only_aggregate_product_data(api_app: FastAPI) -> None:
    api_app.dependency_overrides[get_scholight_admin] = _admin
    metrics = {
        "profiles": {
            "total": 3,
            "active": 2,
            "blocked": 1,
            "admins": 1,
            "created_in_period": 1,
        },
        "searches": {
            "total": 6,
            "authenticated": 4,
            "anonymous": 2,
            "standard": 5,
            "thorough": 1,
            "authenticated_rest": 3,
            "authenticated_mcp": 1,
            "authenticated_success": 3,
            "authenticated_degraded": 1,
            "authenticated_failed": 0,
            "authenticated_p50_response_ms": 100.0,
            "authenticated_p95_response_ms": 200.0,
        },
        "access_keys": {"total": 2, "active": 1, "used_in_period": 1},
        "daily": [],
    }

    with patch(
        "scholight.api.routes.admin_analytics.query_admin_analytics",
        AsyncMock(return_value=metrics),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://test"
        ) as client:
            response = await client.get("/admin/analytics/overview?days=30")

    assert "email" not in response.text and "user_id" not in response.text


@pytest.mark.asyncio
async def test_operations_endpoint_rejects_signed_in_non_admin(api_app: FastAPI) -> None:
    api_app.dependency_overrides[get_current_user] = _admin
    with patch("scholight.api.deps.is_scholight_admin", AsyncMock(return_value=False)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://test"
        ) as client:
            response = await client.get("/admin/operations/overview")

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_analytics_database_error_does_not_expose_internal_detail(api_app: FastAPI) -> None:
    api_app.dependency_overrides[get_scholight_admin] = _admin
    with patch(
        "scholight.api.routes.admin_analytics.query_admin_analytics",
        AsyncMock(side_effect=DBError("postgresql://private-host/internal")),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://test"
        ) as client:
            response = await client.get("/admin/analytics/overview")

    assert "private-host" not in response.text
