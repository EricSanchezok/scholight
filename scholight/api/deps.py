"""FastAPI 依赖注入 — 认证通过 cloud-auth SDK 实现。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from cloud_auth.config import AuthConfig
from cloud_auth.db.asyncpg import AsyncpgUserDatabase
from cloud_auth.dependencies import create_get_current_user as _create_get_current_user
from cloud_auth.models.user import UserRecord
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

security = HTTPBearer(scheme_name="BearerAuth")
optional_security = HTTPBearer(auto_error=False, scheme_name="BearerAuth")

# ── 延迟绑定：cloud-auth SDK ──
# cloud-auth's create_get_current_user returns an async callable that
# resolves to a UserRecord; type the lazy-bound handle so ``await`` type-checks.
_get_current_user_callable: Callable[..., Awaitable[UserRecord]] | None = None


def wire_dependencies(*, db: AsyncpgUserDatabase, auth_config: AuthConfig) -> None:
    """在 create_app() 中调用一次, 连接 cloud-auth SDK 的依赖。"""
    global _get_current_user_callable
    # cloud_auth declares the factory return as Callable[..., object];
    # the actual closure is async and resolves to UserRecord.
    _get_current_user_callable = cast(
        "Callable[..., Awaitable[UserRecord]]",
        _create_get_current_user(db=db, config=auth_config),
    )


async def _resolve_current_user(
    credentials: HTTPAuthorizationCredentials,
) -> UserRecord:
    if _get_current_user_callable is None:
        raise RuntimeError("Dependencies not wired — call wire_dependencies() in create_app()")
    try:
        return await _get_current_user_callable(credentials=credentials)
    except HTTPException as exc:
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
