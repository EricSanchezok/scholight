"""Scholight presentation tests for cloud-auth sessions."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock

import pytest
from cloud_auth.manager import UserManager
from cloud_auth.models.user import SessionRecord

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
