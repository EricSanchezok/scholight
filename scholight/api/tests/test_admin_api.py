"""Quota administration API contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
from cloud_auth.models.user import UserRecord
from fastapi import FastAPI

from scholight.api.deps import get_current_user, get_scholight_admin
from scholight.db.queries_admin import AdminAuditEvent, AdminTarget
from scholight.models.quota import QuotaStatus


def _admin() -> UserRecord:
    return UserRecord(
        id=42,
        email="admin@example.com",
        password_hash="hash",
        status="active",
        email_verified=True,
    )


@pytest.mark.asyncio
async def test_capabilities_are_available_to_non_admin_jwt_user(api_app: FastAPI) -> None:
    api_app.dependency_overrides[get_current_user] = _admin
    with patch("scholight.api.routes.admin.is_scholight_admin", AsyncMock(return_value=False)):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://test"
        ) as client:
            response = await client.get("/admin/capabilities")

    assert response.status_code == 200
    assert response.json() == {
        "can_manage_quotas": False,
        "can_view_operations": False,
        "can_view_analytics": False,
    }


@pytest.mark.asyncio
async def test_lookup_returns_defaults_overrides_effective_and_usage(api_app: FastAPI) -> None:
    api_app.dependency_overrides[get_scholight_admin] = _admin
    target = AdminTarget(
        id=7,
        email="reader@example.com",
        display_name="Reader",
        account_status="active",
        email_verified=True,
    )
    statuses = [
        QuotaStatus(strength="standard", daily_limit=5000, used=20, remaining=4980),
        QuotaStatus(strength="thorough", daily_limit=1000, used=4, remaining=996),
    ]

    with (
        patch(
            "scholight.api.routes.admin.find_admin_target_by_email",
            AsyncMock(return_value=target),
        ),
        patch(
            "scholight.api.routes.admin.get_user_quota_overrides",
            AsyncMock(return_value={"standard": 5000, "thorough": None}),
        ),
        patch(
            "scholight.api.routes.admin.get_user_quota_status",
            AsyncMock(return_value=statuses),
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://test"
        ) as client:
            response = await client.get("/admin/users/lookup?email=reader@example.com")

    assert response.status_code == 200
    assert response.json()["quotas"]["standard"] == {
        "default_limit": 1000,
        "override_limit": 5000,
        "effective_limit": 5000,
        "used": 20,
        "remaining": 4980,
    }


@pytest.mark.asyncio
async def test_lookup_requires_complete_email(api_app: FastAPI) -> None:
    api_app.dependency_overrides[get_scholight_admin] = _admin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as client:
        response = await client.get("/admin/users/lookup?email=reader")

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_lookup_missing_user_explains_exact_email_requirement(api_app: FastAPI) -> None:
    api_app.dependency_overrides[get_scholight_admin] = _admin
    with patch(
        "scholight.api.routes.admin.find_admin_target_by_email",
        AsyncMock(return_value=None),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app),
            base_url="http://test",
        ) as client:
            response = await client.get("/admin/users/lookup?email=missing@example.com")

    assert response.status_code == 404
    assert response.json()["detail"] == {
        "code": "user_not_found",
        "message": "No Scholight user exists with that exact email address.",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_update_requires_both_fields_and_enforces_upper_bound(api_app: FastAPI) -> None:
    api_app.dependency_overrides[get_scholight_admin] = _admin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="http://test"
    ) as client:
        missing = await client.put(
            "/admin/users/7/quota-overrides",
            json={"standard": 100},
        )
        excessive = await client.put(
            "/admin/users/7/quota-overrides",
            json={"standard": 1_000_001, "thorough": None},
        )

    assert missing.status_code == 422
    assert excessive.status_code == 422


@pytest.mark.asyncio
async def test_audit_endpoint_returns_latest_events(api_app: FastAPI) -> None:
    api_app.dependency_overrides[get_scholight_admin] = _admin
    event = AdminAuditEvent(
        event_id=UUID("00000000-0000-0000-0000-000000000001"),
        actor_type="user",
        actor_identifier="admin@example.com",
        target_user_id=7,
        target_email="reader@example.com",
        action="quota_overrides_updated",
        before_state={"standard": 1000, "thorough": None},
        after_state={"standard": 5000, "thorough": None},
        created_at=datetime(2026, 7, 23, tzinfo=UTC),
    )

    with patch(
        "scholight.api.routes.admin.list_admin_audit_events",
        AsyncMock(return_value=[event]),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://test"
        ) as client:
            response = await client.get("/admin/audit-events?limit=20")

    assert response.status_code == 200
    assert response.json()[0]["action"] == "quota_overrides_updated"
