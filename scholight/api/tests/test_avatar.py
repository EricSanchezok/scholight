"""Scholight's read-only shared-avatar presentation contract."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from fastapi import FastAPI
from sanchezcloud_identity.exceptions import AvatarNotFoundError, AvatarStorageError
from sanchezcloud_identity.models.avatar import AvatarView
from sanchezcloud_identity.models.user import UserRecord

from scholight.api.routes.avatar import AvatarReader, get_avatar_read_router


def _app(reader: AvatarReader | None, user: UserRecord) -> FastAPI:
    app = FastAPI()
    app.include_router(
        get_avatar_read_router(
            avatar_reader=reader,
            get_current_user=lambda: user,
        ),
        prefix="/user",
    )
    return app


@pytest.mark.asyncio
async def test_avatar_read_returns_short_lived_identity_view(active_user: UserRecord) -> None:
    view = AvatarView(
        url="https://avatars.example.test/signed",
        version=UUID("11111111-1111-1111-1111-111111111111"),
        expires_at=datetime(2026, 8, 5, 12, 15, tzinfo=UTC),
    )
    reader = AsyncMock(spec=AvatarReader)
    reader.get.return_value = view

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(reader, active_user)),
        base_url="http://test",
    ) as client:
        response = await client.get("/user/avatar")

    assert response.status_code == 200
    assert response.json()["url"] == view.url
    reader.get.assert_awaited_once_with(active_user.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", [False, True])
async def test_missing_or_unconfigured_avatar_is_a_safe_fallback(
    configured: bool,
    active_user: UserRecord,
) -> None:
    reader = AsyncMock(spec=AvatarReader) if configured else None
    if configured:
        assert reader is not None
        reader.get.side_effect = AvatarNotFoundError("private detail")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(reader, active_user)),
        base_url="http://test",
    ) as client:
        response = await client.get("/user/avatar")

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "avatar_not_found"


@pytest.mark.asyncio
async def test_avatar_storage_failure_is_retryable_without_leaking_details(
    active_user: UserRecord,
) -> None:
    reader = AsyncMock(spec=AvatarReader)
    reader.get.side_effect = AvatarStorageError("private S3 detail")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(reader, active_user)),
        base_url="http://test",
    ) as client:
        response = await client.get("/user/avatar")

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["detail"] == {
        "code": "avatar_service_unavailable",
        "message": "The profile photo is temporarily unavailable.",
        "retryable": True,
    }
