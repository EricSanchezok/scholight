"""FastAPI application factory with lifespan-managed PG pool and Zilliz Cloud connectivity."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

_DEPENDENCY_TIMEOUT_SECONDS = 2.0
_DEPENDENCY_CACHE_TTL_SECONDS = 2.0
_dependency_probe_cache: dict[str, tuple[float, bool]] = {}
_dependency_probe_locks: dict[str, asyncio.Lock] = {}


def _reset_dependency_probe_cache() -> None:
    """Clear process-local dependency probe state (used at startup and by tests)."""
    _dependency_probe_cache.clear()
    _dependency_probe_locks.clear()


async def _cached_probe(name: str, probe: Callable[[], Awaitable[bool]]) -> bool:
    now = time.monotonic()
    cached = _dependency_probe_cache.get(name)
    if cached is not None and now - cached[0] < _DEPENDENCY_CACHE_TTL_SECONDS:
        return cached[1]

    lock = _dependency_probe_locks.setdefault(name, asyncio.Lock())
    async with lock:
        now = time.monotonic()
        cached = _dependency_probe_cache.get(name)
        if cached is not None and now - cached[0] < _DEPENDENCY_CACHE_TTL_SECONDS:
            return cached[1]
        result = await probe()
        _dependency_probe_cache[name] = (time.monotonic(), result)
        return result


async def _probe_postgres() -> bool:
    from scholight.db.client import get_pool

    async def execute_probe() -> None:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute("SELECT 1")

    try:
        await asyncio.wait_for(execute_probe(), timeout=_DEPENDENCY_TIMEOUT_SECONDS)
    except Exception:
        return False
    return True


def _list_zilliz_collections() -> None:
    from scholight.store.client import get_client

    get_client().list_collections(timeout=_DEPENDENCY_TIMEOUT_SECONDS)


async def _probe_zilliz() -> bool:
    try:
        await asyncio.wait_for(
            asyncio.to_thread(_list_zilliz_collections), timeout=_DEPENDENCY_TIMEOUT_SECONDS
        )
    except Exception:
        return False
    return True


async def _is_postgres_ready() -> bool:
    return await _cached_probe("postgres", _probe_postgres)


async def _is_zilliz_ready() -> bool:
    return await _cached_probe("zilliz", _probe_zilliz)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle for database connections."""
    from scholight.api.history_tasks import drain_search_history_tasks
    from scholight.api.extract_execution import reset_extract_result_cache
    from scholight.api.search_access import reset_anonymous_minute_limits
    from scholight.api.search_in_flight import reset_search_in_flight_tracker
    from scholight.api.usage_tasks import drain_usage_tasks
    from scholight.db.client import close_pool, create_pool
    from scholight.pipeline.embedder import (
        start_api_embedding_client,
        stop_api_embedding_client,
    )
    from scholight.store.client import get_client

    _reset_dependency_probe_cache()
    reset_extract_result_cache()
    reset_anonymous_minute_limits()
    reset_search_in_flight_tracker()
    async with app.state.mcp_server.session_manager.run():
        await create_pool()
        await start_api_embedding_client()
        with suppress(Exception):
            get_client()
        try:
            yield
        finally:
            await asyncio.gather(drain_search_history_tasks(), drain_usage_tasks())
            await stop_api_embedding_client()
            await close_pool()


_ONE_MB = 1_048_576


async def _limit_body_size(request: Request) -> None:
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > _ONE_MB:
                raise ValueError
        except ValueError:
            raise ValueError("Request body exceeds 1 MB limit") from None


def create_app() -> FastAPI:
    """Build and return the configured FastAPI application."""
    from scholight import __version__
    from scholight.api.mcp_server import create_mcp_app
    from scholight.api.middleware.cors import setup_cors
    from scholight.config import AUTH_CLIENT_ID, settings, validate_api_runtime_settings
    from scholight.logging.middleware import RequestContextMiddleware, TimingMiddleware

    validate_api_runtime_settings()

    app = FastAPI(
        title="Scholight API",
        description="AI-powered academic paper search engine",
        version=__version__,
        lifespan=lifespan,
    )
    mcp_server, mcp_app = create_mcp_app()
    app.state.mcp_server = mcp_server

    setup_cors(app)

    @app.middleware("http")
    async def body_size_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            await _limit_body_size(request)
        except ValueError as exc:
            return JSONResponse(status_code=413, content={"detail": str(exc)})
        return await call_next(request)

    app.add_middleware(TimingMiddleware)
    app.add_middleware(RequestContextMiddleware)

    # ── Route routers ──
    from cloud_auth.config import AuthConfig
    from cloud_auth.db.asyncpg import AsyncpgUserDatabase
    from cloud_auth.manager import UserManager
    from cloud_auth.ratelimit import RegisterRateLimiter
    from cloud_auth.routers import RefreshCookieConfig, get_auth_router, get_user_router

    from scholight.api.deps import get_current_user, wire_dependencies
    from scholight.api.routes.access_keys import router as access_key_router
    from scholight.api.routes.extract import router as extract_router
    from scholight.api.routes.admin import router as admin_router
    from scholight.api.routes.admin_analytics import router as admin_analytics_router
    from scholight.api.routes.admin_operations import router as admin_operations_router
    from scholight.api.routes.search import router as search_router
    from scholight.api.routes.sessions import router as session_router
    from scholight.api.routes.survey import router as survey_router
    from scholight.api.routes.usage import router as usage_router
    from scholight.db.client import get_pool

    auth_config = AuthConfig(
        client_id=AUTH_CLIENT_ID,
        jwt_secret=settings.jwt_secret,
        jwt_access_token_ttl_minutes=settings.jwt_access_token_ttl_minutes,
        jwt_refresh_token_ttl_days=settings.jwt_refresh_token_ttl_days,
        account_lockout_threshold=settings.account_lockout_threshold,
        account_lockout_duration_minutes=settings.account_lockout_duration_minutes,
    )
    db = AsyncpgUserDatabase(pool_factory=lambda: get_pool())

    email_sender = None
    if settings.aliyun_dm_account_name:
        from cloud_auth.email.aliyun import AliyunDirectMailSender

        email_sender = AliyunDirectMailSender(
            access_key_id=settings.aliyun_dm_access_key_id,
            access_key_secret=settings.aliyun_dm_access_key_secret,
            account_name=settings.aliyun_dm_account_name,
            verification_url=f"{settings.public_web_url.rstrip('/')}/verify-email",
            password_reset_url=f"{settings.public_web_url.rstrip('/')}/reset-password",
            from_alias=settings.aliyun_dm_from_alias,
            brand="Scholight",
            reply_to_address=settings.aliyun_dm_reply_to_address,
        )

    user_manager = UserManager(db=db, email_sender=email_sender, config=auth_config)
    wire_dependencies(db=db, auth_config=auth_config, user_manager=user_manager)

    app.include_router(
        get_auth_router(
            user_manager=user_manager,
            get_current_user=get_current_user,
            register_rate_limiter=RegisterRateLimiter(max_attempts=50, window_seconds=3600),
            refresh_cookie=RefreshCookieConfig(
                name="scholight_refresh",
                max_age_seconds=settings.jwt_refresh_token_ttl_days * 24 * 60 * 60,
                secure=settings.auth_refresh_cookie_secure,
                samesite="strict",
                path="/api/auth",
            ),
        ),
        prefix="/auth",
        tags=["auth"],
    )
    app.include_router(
        get_user_router(user_manager=user_manager, get_current_user=get_current_user),
        prefix="/user",
        tags=["user"],
    )
    app.include_router(access_key_router, prefix="/user/access-keys", tags=["access-keys"])
    app.include_router(usage_router, prefix="/user/usage", tags=["usage"])
    app.include_router(session_router, prefix="/auth/sessions", tags=["sessions"])
    app.include_router(survey_router, tags=["survey"])
    app.include_router(admin_router, prefix="/admin", tags=["admin"])
    app.include_router(
        admin_operations_router,
        prefix="/admin/operations",
        tags=["admin-operations"],
    )
    app.include_router(
        admin_analytics_router,
        prefix="/admin/analytics",
        tags=["admin-analytics"],
    )

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        postgres_ready, zilliz_ready = await asyncio.gather(
            _is_postgres_ready(), _is_zilliz_ready()
        )
        is_ready = postgres_ready and zilliz_ready
        return JSONResponse(
            status_code=200 if is_ready else 503,
            content={
                "status": "ready" if is_ready else "unavailable",
                "postgres": "up" if postgres_ready else "down",
                "zilliz": "up" if zilliz_ready else "down",
            },
        )

    @app.get("/health")
    async def health() -> dict[str, str | None]:
        postgres_ready = await _is_postgres_ready()
        return {
            "status": "ok" if postgres_ready else "degraded",
            "pg": None if postgres_ready else "PostgreSQL unreachable",
        }

    app.include_router(search_router, prefix="/search", tags=["search"])
    app.include_router(extract_router, prefix="/extract", tags=["extract"])
    # Keep /mcp exact: a nested /mcp mount redirects to /mcp/, which drops the
    # public /api prefix after Caddy's handle_path rewrite.
    app.mount("/", mcp_app, name="mcp")

    return app
