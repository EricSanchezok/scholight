"""Client-scoped session management backed entirely by cloud-auth."""

from __future__ import annotations

from cloud_auth.exceptions import AuthError, DBError
from cloud_auth.manager import UserManager
from cloud_auth.models.auth import MessageResponse
from cloud_auth.models.user import UserRecord
from fastapi import APIRouter, Depends
from fastapi.security import HTTPAuthorizationCredentials

from scholight.api.deps import get_current_user, get_user_manager, security
from scholight.api.http_errors import http_error
from scholight.api.sessions import SessionResponse, list_user_sessions

router = APIRouter()


def _session_id(
    credentials: HTTPAuthorizationCredentials,
    manager: UserManager,
) -> int:
    try:
        return manager.session_id_from_access_token(credentials.credentials)
    except AuthError as exc:
        raise http_error(
            401,
            code="session_context_unavailable",
            message="Sign in again before managing sessions.",
            retryable=False,
            retry_after=None,
        ) from exc


@router.get("", response_model=list[SessionResponse])
async def get_sessions(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    current_user: UserRecord = Depends(get_current_user),
    manager: UserManager = Depends(get_user_manager),
) -> list[SessionResponse]:
    try:
        return await list_user_sessions(
            manager,
            current_user.id,
            current_session_id=_session_id(credentials, manager),
        )
    except DBError as exc:
        raise http_error(
            503,
            code="session_service_unavailable",
            message="Session management is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc


@router.delete("/{session_id}", response_model=MessageResponse)
async def delete_session(
    session_id: int,
    current_user: UserRecord = Depends(get_current_user),
    manager: UserManager = Depends(get_user_manager),
) -> MessageResponse:
    try:
        revoked = await manager.revoke_session(current_user.id, session_id)
    except DBError as exc:
        raise http_error(
            503,
            code="session_service_unavailable",
            message="Session management is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    if not revoked:
        raise http_error(
            404,
            code="session_not_found",
            message="This session no longer exists or has already been revoked.",
            retryable=False,
            retry_after=None,
        )
    return MessageResponse(message="Session revoked")


@router.post("/revoke-others", response_model=MessageResponse)
async def revoke_others(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    current_user: UserRecord = Depends(get_current_user),
    manager: UserManager = Depends(get_user_manager),
) -> MessageResponse:
    current_session_id = _session_id(credentials, manager)
    try:
        await manager.revoke_other_sessions(current_user.id, current_session_id)
    except DBError as exc:
        raise http_error(
            503,
            code="session_service_unavailable",
            message="Session management is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    return MessageResponse(message="Other sessions revoked")


__all__ = ["router"]
