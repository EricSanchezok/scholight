"""Scholight presentation models for sanchezcloud-identity product sessions."""

from __future__ import annotations

from sanchezcloud_identity.manager import UserManager
from sanchezcloud_identity.models.user import SessionRecord


class SessionResponse(SessionRecord):
    current: bool


async def list_user_sessions(
    manager: UserManager,
    user_id: int,
    *,
    current_session_id: int,
) -> list[SessionResponse]:
    records = await manager.list_sessions(user_id)
    return [
        SessionResponse(**record.model_dump(), current=record.id == current_session_id)
        for record in records
    ]


__all__ = ["SessionResponse", "list_user_sessions"]
