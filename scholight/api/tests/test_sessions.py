"""Scholight presentation tests for sanchezcloud-identity sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from sanchezcloud_identity.exceptions import DBError
from sanchezcloud_identity.manager import UserManager
from sanchezcloud_identity.models.user import SessionRecord, UserRecord

from scholight.api.deps import get_current_user, get_user_manager
from scholight.api.sessions import list_user_sessions


@pytest.mark.asyncio
async def test_session_list_marks_only_access_token_session_as_current() -> None:
    records = [
        SessionRecord(
            id=100,
            created_at=datetime(2026, 7, 20, tzinfo=UTC),
            last_seen_at=datetime(2026, 7, 22, tzinfo=UTC),
            expires_at=datetime(2026, 7, 27, tzinfo=UTC),
            user_agent="Browser A",
        ),
        SessionRecord(
            id=200,
            created_at=datetime(2026, 7, 21, tzinfo=UTC),
            last_seen_at=datetime(2026, 7, 23, tzinfo=UTC),
            expires_at=datetime(2026, 7, 28, tzinfo=UTC),
            user_agent=None,
        ),
    ]
    manager = Mock(spec=UserManager)
    manager.list_sessions = AsyncMock(return_value=records)

    sessions = await list_user_sessions(manager, 42, current_session_id=200)

    assert [session.current for session in sessions] == [False, True]
    manager.list_sessions.assert_awaited_once_with(42)


@pytest.mark.asyncio
async def test_session_database_failure_is_explicitly_retryable(
    api_app: FastAPI,
    active_user: UserRecord,
) -> None:
    manager = Mock(spec=UserManager)
    manager.session_id_from_access_token.return_value = 100
    api_app.dependency_overrides[get_current_user] = lambda: active_user
    api_app.dependency_overrides[get_user_manager] = lambda: manager

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(
            "scholight.api.routes.sessions.list_user_sessions",
            AsyncMock(side_effect=DBError("private SQL detail")),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app),
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/auth/sessions",
                headers={"Authorization": "Bearer access-token"},
            )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["detail"] == {
        "code": "session_service_unavailable",
        "message": "Session management is temporarily unavailable.",
        "retryable": True,
    }


@pytest.mark.asyncio
async def test_missing_session_explains_that_it_may_already_be_revoked(
    api_app: FastAPI,
    active_user: UserRecord,
) -> None:
    manager = Mock(spec=UserManager)
    manager.revoke_session = AsyncMock(return_value=False)
    api_app.dependency_overrides[get_current_user] = lambda: active_user
    api_app.dependency_overrides[get_user_manager] = lambda: manager

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app),
        base_url="http://test",
    ) as client:
        response = await client.delete("/auth/sessions/100")

    assert response.status_code == 404
    assert response.json()["detail"]["message"] == (
        "This session no longer exists or has already been revoked."
    )
