"""FastAPI 依赖注入 — 认证通过 cloud-auth SDK 实现。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from cloud_auth.config import AuthConfig
from cloud_auth.db.asyncpg import AsyncpgUserDatabase
from cloud_auth.dependencies import create_get_current_user as _create_get_current_user
from cloud_auth.exceptions import AuthError, DBError as AuthDBError
from cloud_auth.manager import UserManager
from cloud_auth.models.user import UserRecord
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from scholight.api.access_keys import AccessKeyError, resolve_access_key
from scholight.db.client import DBError
from scholight.db.queries_profile import ProductAccessBlockedError, ensure_product_access

security = HTTPBearer(scheme_name="BearerAuth")
optional_security = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


@dataclass(frozen=True, slots=True)
class SearchActor:
    """Authenticated identity used only by the public search surface."""

    user: UserRecord
    actor_type: Literal["web", "access_key"]
    access_key_id: UUID | None = None


# ── 延迟绑定：cloud-auth SDK ──
# cloud-auth's create_get_current_user returns an async callable that
# resolves to a UserRecord; type the lazy-bound handle so ``await`` type-checks.
_get_current_user_callable: Callable[..., Awaitable[UserRecord]] | None = None
_user_manager: UserManager | None = None


def wire_dependencies(
    *,
    db: AsyncpgUserDatabase,
    auth_config: AuthConfig,
    user_manager: UserManager,
) -> None:
    """在 create_app() 中调用一次, 连接 cloud-auth SDK 的依赖。"""
    global _get_current_user_callable, _user_manager
    # cloud_auth declares the factory return as Callable[..., object];
    # the actual closure is async and resolves to UserRecord.
    _get_current_user_callable = cast(
        "Callable[..., Awaitable[UserRecord]]",
        _create_get_current_user(db=db, config=auth_config),
    )
    _user_manager = user_manager


def get_user_manager() -> UserManager:
    if _user_manager is None:
        raise RuntimeError("Dependencies not wired — call wire_dependencies() in create_app()")
    return _user_manager


async def _resolve_current_user(
    credentials: HTTPAuthorizationCredentials,
) -> UserRecord:
    if _get_current_user_callable is None:
        raise RuntimeError("Dependencies not wired — call wire_dependencies() in create_app()")
    try:
        user = await _get_current_user_callable(credentials=credentials)
        if _user_manager is None:
            raise RuntimeError("Dependencies not wired — call wire_dependencies() in create_app()")
        session_id = _user_manager.session_id_from_access_token(credentials.credentials)
        if not await _user_manager.touch_session(user.id, session_id):
            raise HTTPException(
                status_code=401,
                detail="Session revoked or expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        try:
            await ensure_product_access(user.id)
        except ProductAccessBlockedError as exc:
            raise HTTPException(status_code=403, detail="Scholight access is blocked") from exc
        return user
    except (AuthDBError, DBError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Authentication service unavailable",
            headers={"Retry-After": "5"},
        ) from exc
    except (AuthError, HTTPException) as exc:
        if isinstance(exc, AuthError):
            raise HTTPException(
                status_code=401,
                detail=str(exc),
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        if exc.status_code != 401:
            raise
        headers = dict(exc.headers or {})
        headers.setdefault("WWW-Authenticate", "Bearer")
        raise HTTPException(status_code=401, detail=exc.detail, headers=headers) from exc


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserRecord:
    return await _resolve_current_user(credentials)


async def get_optional_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
) -> UserRecord | None:
    """Return anonymous only when the Authorization header is completely absent."""
    if "authorization" not in request.headers:
        return None
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail="Invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await _resolve_current_user(credentials)


async def get_optional_search_actor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
) -> SearchActor | None:
    """Authenticate search with either a web JWT or a search-only access key."""
    if "authorization" not in request.headers:
        return None
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail="Invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = credentials.credentials
    if token.startswith("sk_live_"):
        try:
            return await resolve_access_key_search_actor(token)
        except AccessKeyError as exc:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": exc.code,
                    "message": "Access key is invalid or unavailable.",
                    "retryable": False,
                },
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        except DBError as exc:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "access_key_service_unavailable",
                    "message": "Access key service is temporarily unavailable.",
                    "retryable": True,
                },
                headers={"Retry-After": "5"},
            ) from exc
    user = await _resolve_current_user(credentials)
    return SearchActor(user=user, actor_type="web")


async def resolve_access_key_search_actor(token: str) -> SearchActor:
    """Resolve a search-only access key without imposing a transport error model."""
    record, user = await resolve_access_key(token)
    return SearchActor(user=user, actor_type="access_key", access_key_id=record.id)
