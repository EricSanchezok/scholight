"""Optional search authentication behavior tests."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from cloud_auth.models.user import UserRecord
from fastapi import Depends, FastAPI, HTTPException

from scholight.api import deps
from scholight.api.deps import get_optional_current_user


@pytest.fixture
def auth_app(monkeypatch: pytest.MonkeyPatch) -> tuple[FastAPI, AsyncMock]:
    resolver = AsyncMock()
    monkeypatch.setattr(deps, "_get_current_user_callable", resolver)
    app = FastAPI()

    @app.get("/")
    async def identity(
        current_user: UserRecord | None = Depends(get_optional_current_user),
    ) -> dict[str, int | str]:
        if current_user is None:
            return {"identity": "anonymous"}
        return {"identity": "user", "user_id": current_user.id}

    return app, resolver


@pytest.mark.asyncio
async def test_missing_authorization_is_anonymous_without_auth_lookup(
    auth_app: tuple[FastAPI, AsyncMock],
) -> None:
    app, resolver = auth_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")

    assert response.json() == {"identity": "anonymous"}
    resolver.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("authorization", ["Basic abc", "", "Bearer"])
async def test_present_invalid_authorization_never_downgrades_to_anonymous(
    auth_app: tuple[FastAPI, AsyncMock], authorization: str
) -> None:
    app, resolver = auth_app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/", headers={"Authorization": authorization})

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_bearer_authorization_reuses_cloud_auth_resolver(
    auth_app: tuple[FastAPI, AsyncMock], active_user: UserRecord
) -> None:
    app, resolver = auth_app
    resolver.return_value = active_user

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/", headers={"Authorization": "Bearer access-token"})

    assert response.json() == {"identity": "user", "user_id": 42}
    credentials = resolver.await_args.kwargs["credentials"]
    assert (credentials.scheme, credentials.credentials) == ("Bearer", "access-token")


@pytest.mark.asyncio
async def test_cloud_auth_status_error_is_preserved(
    auth_app: tuple[FastAPI, AsyncMock],
) -> None:
    app, resolver = auth_app
    resolver.side_effect = HTTPException(status_code=403, detail="Account disabled")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/", headers={"Authorization": "Bearer access-token"})

    assert response.status_code == 403
    assert response.json() == {"detail": "Account disabled"}
