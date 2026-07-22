"""Refresh-family session identity and management tests."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from cloud_auth.config import AuthConfig
from cloud_auth.jwt import verify_access_token

from scholight.api.sessions import (
    SessionRecord,
    create_session_access_token,
    list_user_sessions,
)


def test_session_access_token_contains_backward_compatible_sid() -> None:
    config = AuthConfig(client_id="scholight", jwt_secret="j" * 32)

    token = create_session_access_token(42, "active@example.com", session_id=123, config=config)
    payload = verify_access_token(token, config=config)

    assert payload["sid"] == 123
    assert payload["sub"] == "42"
    assert payload["aud"] == "scholight"


@pytest.mark.asyncio
async def test_session_list_marks_only_sid_as_current() -> None:
    records = [
        SessionRecord(
            id=100,
            created_at=datetime(2026, 7, 20, tzinfo=UTC),
            last_seen_at=datetime(2026, 7, 22, tzinfo=UTC),
            expires_at=datetime(2026, 7, 27, tzinfo=UTC),
            user_agent="Browser A",
            revoked_at=None,
        ),
        SessionRecord(
            id=200,
            created_at=datetime(2026, 7, 21, tzinfo=UTC),
            last_seen_at=None,
            expires_at=datetime(2026, 7, 28, tzinfo=UTC),
            user_agent=None,
            revoked_at=None,
        ),
    ]

    with patch("scholight.api.sessions.query_sessions", AsyncMock(return_value=records)):
        sessions = await list_user_sessions(
            42,
            client_id="scholight",
            current_session_id=200,
        )

    assert [session.current for session in sessions] == [False, True]


@pytest.mark.asyncio
async def test_legacy_access_token_marks_no_session_current() -> None:
    record = SessionRecord(
        id=100,
        created_at=datetime(2026, 7, 20, tzinfo=UTC),
        last_seen_at=None,
        expires_at=datetime(2026, 7, 27, tzinfo=UTC),
        user_agent=None,
        revoked_at=None,
    )

    with patch("scholight.api.sessions.query_sessions", AsyncMock(return_value=[record])):
        sessions = await list_user_sessions(
            42,
            client_id="scholight",
            current_session_id=None,
        )

    assert not sessions[0].current
