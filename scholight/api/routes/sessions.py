"""JWT-only refresh-family session management routes."""

from __future__ import annotations

from cloud_auth.config import AuthConfig
from cloud_auth.models.auth import MessageResponse
from cloud_auth.models.user import UserRecord
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from scholight.api.deps import get_current_user, security
from scholight.api.sessions import (
    SessionResponse,
    list_user_sessions,
    session_id_from_access_token,
)
from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.queries_sessions import revoke_other_sessions, revoke_session

router = APIRouter()


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": False},
    )


def _session_id(credentials: HTTPAuthorizationCredentials) -> int | None:
    config = AuthConfig(
        jwt_secret=settings.jwt_secret,
        jwt_access_token_ttl_minutes=settings.jwt_access_token_ttl_minutes,
        jwt_refresh_token_ttl_days=settings.jwt_refresh_token_ttl_days,
    )
    return session_id_from_access_token(credentials.credentials, config=config)


@router.get("", response_model=list[SessionResponse])
async def get_sessions(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    current_user: UserRecord = Depends(get_current_user),
) -> list[SessionResponse]:
    try:
        return await list_user_sessions(
            current_user.id, current_session_id=_session_id(credentials)
        )
    except DBError as exc:
        raise _error(503, "session_service_unavailable", "Session service unavailable.") from exc


@router.delete("/{session_id}", response_model=MessageResponse)
async def delete_session(
    session_id: int,
    current_user: UserRecord = Depends(get_current_user),
) -> MessageResponse:
    try:
        revoked = await revoke_session(user_id=current_user.id, session_id=session_id)
    except DBError as exc:
        raise _error(503, "session_service_unavailable", "Session service unavailable.") from exc
    if not revoked:
        raise _error(404, "session_not_found", "Session not found.")
    return MessageResponse(message="Session revoked")


@router.post("/revoke-others", response_model=MessageResponse)
async def revoke_others(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    current_user: UserRecord = Depends(get_current_user),
) -> MessageResponse:
    current_session_id = _session_id(credentials)
    if current_session_id is None:
        raise _error(
            409,
            "session_context_unavailable",
            "Sign in again before revoking other sessions.",
        )
    try:
        await revoke_other_sessions(
            user_id=current_user.id,
            current_session_id=current_session_id,
        )
    except DBError as exc:
        raise _error(503, "session_service_unavailable", "Session service unavailable.") from exc
    return MessageResponse(message="Other sessions revoked")


__all__ = ["router"]
