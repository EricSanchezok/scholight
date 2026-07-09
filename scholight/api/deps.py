"""FastAPI 依赖注入 — 认证通过 cloud-auth SDK 实现。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import cast

from cloud_auth.config import AuthConfig
from cloud_auth.db.asyncpg import AsyncpgUserDatabase
from cloud_auth.dependencies import create_get_current_user as _create_get_current_user
from cloud_auth.models.user import UserRecord
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from slowapi import Limiter
from slowapi.util import get_remote_address

security = HTTPBearer()

# ── 慢速API限流器（Scholight 管理） ──
limiter = Limiter(key_func=get_remote_address)

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


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserRecord:
    """所有受保护路由的 Depends 入口。"""
    if _get_current_user_callable is None:
        raise RuntimeError("Dependencies not wired — call wire_dependencies() in create_app()")
    return await _get_current_user_callable(credentials=credentials)


def api_error(status_code: int, detail: str) -> HTTPException:
    """Shorthand for HTTPException."""
    return HTTPException(status_code=status_code, detail=detail)
