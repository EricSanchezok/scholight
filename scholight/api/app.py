"""FastAPI application factory with lifespan-managed PG pool and Zilliz Cloud connectivity."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Startup / shutdown lifecycle for database connections."""
    from scholight.db.client import close_pool, create_pool
    from scholight.store.client import get_client

    await create_pool()
    with suppress(Exception):
        get_client()
    yield
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
    from scholight.api.middleware.cors import setup_cors
    from scholight.logging.middleware import RequestContextMiddleware, TimingMiddleware

    app = FastAPI(
        title="Scholight API",
        description="AI-powered academic paper search and survey engine",
        version=__version__,
        lifespan=lifespan,
    )

    setup_cors(app)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(TimingMiddleware)

    @app.middleware("http")
    async def body_size_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        try:
            await _limit_body_size(request)
        except ValueError as exc:
            return JSONResponse(status_code=413, content={"detail": str(exc)})
        return await call_next(request)

    # ── Route routers ──
    from cloud_auth.config import AuthConfig
    from cloud_auth.db.asyncpg import AsyncpgUserDatabase
    from cloud_auth.manager import UserManager
    from cloud_auth.ratelimit import RegisterRateLimiter
    from cloud_auth.routers import get_auth_router, get_user_router

    from scholight.api.deps import get_current_user, wire_dependencies
    from scholight.api.routes.search import router as search_router
    from scholight.config import settings
    from scholight.db.client import get_pool

    auth_config = AuthConfig(
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
            from_alias=settings.aliyun_dm_from_alias,
            reply_to_address=settings.aliyun_dm_reply_to_address,
        )

    user_manager = UserManager(db=db, email_sender=email_sender, config=auth_config)
    wire_dependencies(db=db, auth_config=auth_config)

    app.include_router(
        get_auth_router(
            user_manager=user_manager,
            get_current_user=get_current_user,
            register_rate_limiter=RegisterRateLimiter(max_success=50, window_seconds=3600),
        ),
        prefix="/auth",
        tags=["auth"],
    )
    app.include_router(
        get_user_router(user_manager=user_manager, get_current_user=get_current_user),
        prefix="/user",
        tags=["user"],
    )

    @app.get("/health")
    async def health() -> dict[str, str | None]:
        from asyncio import wait_for

        from scholight.db.client import get_pool

        status = "ok"
        pg_error: str | None = None

        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                await wait_for(conn.execute("SELECT 1"), timeout=2.0)
        except Exception:
            status = "degraded"
            pg_error = "PostgreSQL unreachable"

        return {"status": status, "pg": pg_error}

    app.include_router(search_router, prefix="/search", tags=["search"])

    return app
