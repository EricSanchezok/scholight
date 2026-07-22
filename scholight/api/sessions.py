"""Session-aware JWT issuance backed by cloud-auth refresh-token families."""

from __future__ import annotations

from contextvars import ContextVar, Token
from datetime import UTC, datetime, timedelta
from typing import cast

import structlog
from cloud_auth.config import AuthConfig
from cloud_auth.db.base import AbstractUserDatabase
from cloud_auth.email.base import AbstractEmailSender
from cloud_auth.jwt import decode_refresh_token, verify_access_token
from cloud_auth.manager import UserManager
from cloud_auth.models.user import UserRecord
from jose import jwt
from pydantic import BaseModel, ConfigDict

from scholight.db.client import DBError
from scholight.db.queries_sessions import query_sessions, register_session_metadata

logger = structlog.get_logger(__name__)
_session_user_agent: ContextVar[str | None] = ContextVar("session_user_agent", default=None)


class SessionRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: int
    created_at: datetime
    last_seen_at: datetime | None
    expires_at: datetime
    user_agent: str | None
    revoked_at: datetime | None


class SessionResponse(SessionRecord):
    current: bool


def set_session_user_agent(value: str | None) -> Token[str | None]:
    return _session_user_agent.set(value[:512] if value else None)


def reset_session_user_agent(token: Token[str | None]) -> None:
    _session_user_agent.reset(token)


def create_session_access_token(
    user_id: int,
    email: str,
    *,
    session_id: int,
    config: AuthConfig,
) -> str:
    """Issue a normal cloud-auth access token with an additive sid claim."""
    now = datetime.now(UTC)
    payload: dict[str, object] = {
        "sub": str(user_id),
        "email": email,
        "aud": config.client_id,
        "sid": session_id,
        "iat": now,
        "exp": now + timedelta(minutes=config.jwt_access_token_ttl_minutes),
    }
    return cast(str, jwt.encode(payload, config.jwt_secret, algorithm=config.jwt_algorithm))


def session_id_from_access_token(token: str, *, config: AuthConfig) -> int | None:
    payload = verify_access_token(token, config=config)
    sid = payload.get("sid")
    if isinstance(sid, int) and sid > 0:
        return sid
    return None


class ScholightUserManager(UserManager):
    """Cloud-auth manager that adds session metadata and sid-aware access JWTs."""

    def __init__(
        self,
        db: AbstractUserDatabase,
        email_sender: AbstractEmailSender | None,
        config: AuthConfig,
    ) -> None:
        super().__init__(db=db, email_sender=email_sender, config=config)
        self._session_db = db
        self._session_config = config

    async def _finalize_tokens(self, refresh_token: str) -> tuple[str, int, UserRecord]:
        payload = decode_refresh_token(refresh_token, config=self._session_config)
        user_id = int(str(payload["sub"]))
        family_id = int(str(payload["family_id"]))
        user = await self._session_db.get_user_by_id(user_id)
        if user is None:
            raise RuntimeError("Authenticated session owner disappeared")
        try:
            await register_session_metadata(
                user_id=user_id,
                session_id=family_id,
                client_id=self._session_config.client_id,
                user_agent=_session_user_agent.get(),
            )
        except DBError:
            logger.warning("session_metadata_unavailable", user_id=user_id)
        access_token = create_session_access_token(
            user_id,
            str(user.email),
            session_id=family_id,
            config=self._session_config,
        )
        return access_token, family_id, user

    async def login(self, email: str, password: str) -> tuple[str, str]:
        _legacy_access, refresh_token = await super().login(email, password)
        access_token, _family_id, _user = await self._finalize_tokens(refresh_token)
        return access_token, refresh_token

    async def refresh_token(self, refresh_token_str: str) -> tuple[str, str]:
        _legacy_access, new_refresh = await super().refresh_token(refresh_token_str)
        access_token, _family_id, _user = await self._finalize_tokens(new_refresh)
        return access_token, new_refresh


async def list_user_sessions(
    user_id: int,
    *,
    client_id: str,
    current_session_id: int | None,
) -> list[SessionResponse]:
    records = await query_sessions(user_id, client_id=client_id)
    return [
        SessionResponse(**record.model_dump(), current=record.id == current_session_id)
        for record in records
    ]


__all__ = [
    "ScholightUserManager",
    "SessionRecord",
    "SessionResponse",
    "create_session_access_token",
    "list_user_sessions",
    "reset_session_user_agent",
    "session_id_from_access_token",
    "set_session_user_agent",
]
