"""FastAPI 依赖注入 — 认证通过 sanchezcloud-identity SDK 实现。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, cast
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sanchezcloud_identity.config import AuthConfig
from sanchezcloud_identity.db.asyncpg import AsyncpgUserDatabase
from sanchezcloud_identity.dependencies import create_get_current_user as _create_get_current_user
from sanchezcloud_identity.exceptions import AuthError, DBError as AuthDBError
from sanchezcloud_identity.manager import UserManager
from sanchezcloud_identity.models.user import UserRecord

from scholight.api.access_keys import (
    AccessKeyError,
    access_key_error_message,
    resolve_access_key,
)
from scholight.api.http_errors import http_error
from scholight.db.client import DBError
from scholight.db.queries_admin import is_scholight_admin
from scholight.db.queries_profile import ProductAccessBlockedError, ensure_product_access

security = HTTPBearer(scheme_name="BearerAuth")
optional_security = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


@dataclass(frozen=True, slots=True)
class SearchActor:
    """Authenticated identity used only by the public search surface."""

    user: UserRecord
    actor_type: Literal["web", "access_key", "delegated"]
    access_key_id: UUID | None = None
    survey_job_id: UUID | None = None


# ── 延迟绑定：sanchezcloud-identity SDK ──
# sanchezcloud-identity's create_get_current_user returns an async callable that
# resolves to a UserRecord; type the lazy-bound handle so ``await`` type-checks.
_get_current_user_callable: Callable[..., Awaitable[UserRecord]] | None = None
_user_manager: UserManager | None = None
_user_db: AsyncpgUserDatabase | None = None


class DelegationError(Exception):
    def __init__(self, code: str, *, status_code: int = 401) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code


def wire_dependencies(
    *,
    db: AsyncpgUserDatabase,
    auth_config: AuthConfig,
    user_manager: UserManager,
) -> None:
    """在 create_app() 中调用一次, 连接 sanchezcloud-identity SDK 的依赖。"""
    global _get_current_user_callable, _user_manager, _user_db
    # sanchezcloud_identity declares the factory return as Callable[..., object];
    # the actual closure is async and resolves to UserRecord.
    _get_current_user_callable = cast(
        "Callable[..., Awaitable[UserRecord]]",
        _create_get_current_user(db=db, config=auth_config),
    )
    _user_manager = user_manager
    _user_db = db


def get_user_manager() -> UserManager:
    if _user_manager is None:
        raise RuntimeError("Dependencies not wired — call wire_dependencies() in create_app()")
    return _user_manager


def _bearer_error(*, code: str, message: str) -> HTTPException:
    error = http_error(
        401,
        code=code,
        message=message,
        retryable=False,
        retry_after=None,
    )
    error.headers = {"WWW-Authenticate": "Bearer"}
    return error


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
            raise _bearer_error(
                code="authentication_required",
                message="Your session has expired or been revoked. Sign in again.",
            )
        try:
            await ensure_product_access(user.id)
        except ProductAccessBlockedError as exc:
            raise http_error(
                403,
                code="product_access_blocked",
                message="Scholight access for this account is blocked.",
                retryable=False,
                retry_after=None,
            ) from exc
        return user
    except (AuthDBError, DBError) as exc:
        raise http_error(
            503,
            code="authentication_service_unavailable",
            message="Authentication is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    except (AuthError, HTTPException) as exc:
        if isinstance(exc, AuthError):
            raise _bearer_error(
                code="authentication_required",
                message="Your session is invalid or has expired. Sign in again.",
            ) from exc
        if exc.status_code != 401:
            raise
        if isinstance(exc.detail, dict):
            headers = dict(exc.headers or {})
            headers.setdefault("WWW-Authenticate", "Bearer")
            raise HTTPException(
                status_code=401,
                detail=exc.detail,
                headers=headers,
            ) from exc
        raise _bearer_error(
            code="authentication_required",
            message="Your session is invalid or has expired. Sign in again.",
        ) from exc


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> UserRecord:
    return await _resolve_current_user(credentials)


async def get_scholight_admin(
    current_user: UserRecord = Depends(get_current_user),
) -> UserRecord:
    """Require current, database-backed Scholight product administration."""
    try:
        permitted = await is_scholight_admin(current_user.id)
    except DBError as exc:
        raise http_error(
            503,
            code="admin_service_unavailable",
            message="Administration service is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    if not permitted:
        raise http_error(
            403,
            code="admin_required",
            message="Scholight administrator permission is required.",
            retryable=False,
            retry_after=None,
        )
    return current_user


async def get_optional_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
) -> UserRecord | None:
    """Return anonymous only when the Authorization header is completely absent."""
    if "authorization" not in request.headers:
        return None
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise _bearer_error(
            code="invalid_access_token",
            message="Use a valid Bearer access token.",
        )
    return await _resolve_current_user(credentials)


async def get_optional_search_actor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
) -> SearchActor | None:
    """Authenticate search with either a web JWT or an all-tools access key."""
    if "authorization" not in request.headers:
        return None
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise _bearer_error(
            code="invalid_access_token",
            message="Use a valid Bearer access token.",
        )
    token = credentials.credentials
    if token.startswith("sk_live_"):
        try:
            return await resolve_access_key_search_actor(token)
        except AccessKeyError as exc:
            status_code = 403 if exc.code == "product_access_blocked" else 401
            error = http_error(
                status_code,
                code=exc.code,
                message=access_key_error_message(exc.code),
                retryable=False,
                retry_after=None,
            )
            if status_code == 401:
                error.headers = {"WWW-Authenticate": "Bearer"}
            raise error from exc
        except DBError as exc:
            raise http_error(
                503,
                code="access_key_service_unavailable",
                message="Access key authentication is temporarily unavailable.",
                retryable=True,
                retry_after=5,
            ) from exc
    user = await _resolve_current_user(credentials)
    return SearchActor(user=user, actor_type="web")


async def resolve_access_key_search_actor(token: str) -> SearchActor:
    """Resolve an all-tools access key without imposing a transport error model."""
    record, user = await resolve_access_key(token)
    return SearchActor(user=user, actor_type="access_key", access_key_id=record.id)


async def get_extract_actor(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(optional_security),
) -> SearchActor:
    """Authenticate Web Extract with an all-tools Access Key or approved delegation."""
    if "authorization" not in request.headers or credentials is None:
        raise _bearer_error(
            code="authentication_required",
            message="Use a Scholight Access Key as a Bearer credential.",
        )
    token = credentials.credentials
    if credentials.scheme.lower() != "bearer" or not token:
        raise _bearer_error(
            code="invalid_access_key",
            message="Access key is invalid.",
        )
    try:
        if token.startswith("sk_live_"):
            return await resolve_access_key_search_actor(token)
        return await resolve_delegated_search_actor(token)
    except AccessKeyError as exc:
        status_code = 403 if exc.code == "product_access_blocked" else 401
        error = http_error(
            status_code,
            code=exc.code,
            message=access_key_error_message(exc.code),
            retryable=False,
            retry_after=None,
        )
        if status_code == 401:
            error.headers = {"WWW-Authenticate": "Bearer"}
        raise error from exc
    except DelegationError as exc:
        raise http_error(
            exc.status_code,
            code=exc.code,
            message="Delegated identity is invalid or unavailable.",
            retryable=exc.status_code == 503,
            retry_after=5 if exc.status_code == 503 else None,
        ) from exc
    except DBError as exc:
        raise http_error(
            503,
            code="access_key_service_unavailable",
            message="Access key authentication is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc


async def resolve_delegated_search_actor(token: str) -> SearchActor:
    """Verify an approved product delegation and resolve its shared user."""
    from scholight.config import settings

    credentials = (
        (settings.mcp_delegation_jwt_secret, "scholens"),
        (settings.survey_mcp_jwt_secret, "scholight-survey"),
    )
    claims: dict[str, Any] | None = None
    last_error: jwt.PyJWTError | None = None
    for secret, issuer in credentials:
        if len(secret.encode("utf-8")) < 32:
            continue
        try:
            claims = jwt.decode(
                token,
                secret,
                algorithms=["HS256"],
                audience="scholight-mcp",
                issuer=issuer,
                options={"require": ["sub", "scope", "iat", "exp", "jti"]},
            )
            break
        except jwt.PyJWTError as exc:
            last_error = exc
    if claims is None:
        raise DelegationError("invalid_delegation") from last_error

    try:
        if claims.get("scope") not in {"mcp", "search"}:
            raise DelegationError("invalid_delegation")
        user_id = int(claims["sub"])
        raw_survey_job_id = claims.get("survey_job_id")
        if raw_survey_job_id is not None and claims.get("iss") != "scholight-survey":
            raise DelegationError("invalid_delegation")
        survey_job_id = UUID(str(raw_survey_job_id)) if raw_survey_job_id is not None else None
    except DelegationError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise DelegationError("invalid_delegation") from exc

    if _user_db is None:
        raise RuntimeError("Dependencies not wired — call wire_dependencies() in create_app()")
    try:
        user = await _user_db.get_user_by_id(user_id)
        if user is None or user.status != "active":
            raise DelegationError("delegation_user_unavailable", status_code=403)
        await ensure_product_access(user.id)
    except ProductAccessBlockedError as exc:
        raise DelegationError("scholight_access_blocked", status_code=403) from exc
    except (AuthDBError, DBError) as exc:
        raise DelegationError("delegation_service_unavailable", status_code=503) from exc
    return SearchActor(
        user=user,
        actor_type="delegated",
        survey_job_id=survey_job_id,
    )
