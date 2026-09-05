"""PostgreSQL connection pool lifecycle — asyncpg pool manager with SSL."""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from types import TracebackType
from typing import Any, cast

import asyncpg
import structlog

from scholight.config import settings

logger = structlog.get_logger(__name__)

# ── Custom exceptions ──────────────────────────────────────────────────────


class DBError(Exception):
    """Base exception for database-layer errors."""


# ── Module-level pool ──────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None
_bound_pool: ContextVar[_PinnedConnectionPool | None] = ContextVar(
    "scholight_bound_database_pool",
    default=None,
)


class _PinnedAcquire:
    """Serialize an ``acquire`` block onto one already-owned connection."""

    def __init__(self, pool: _PinnedConnectionPool) -> None:
        self._pool = pool

    async def __aenter__(self) -> asyncpg.Connection:
        await self._pool.operation_lock.acquire()
        return self._pool.connection

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._pool.operation_lock.release()


class _PinnedConnectionPool:
    """Pool-shaped adapter that keeps a control cycle on one PostgreSQL session."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self.connection = connection
        self.operation_lock = asyncio.Lock()

    def acquire(self) -> _PinnedAcquire:
        return _PinnedAcquire(self)

    async def execute(
        self,
        query: str,
        *args: Any,
        timeout: float | None = None,
    ) -> str:
        async with self.operation_lock:
            return cast(str, await self.connection.execute(query, *args, timeout=timeout))

    async def fetch(
        self,
        query: str,
        *args: Any,
        timeout: float | None = None,
    ) -> list[asyncpg.Record]:
        async with self.operation_lock:
            return cast(
                list[asyncpg.Record],
                await self.connection.fetch(query, *args, timeout=timeout),
            )

    async def fetchrow(
        self,
        query: str,
        *args: Any,
        timeout: float | None = None,
    ) -> asyncpg.Record | None:
        async with self.operation_lock:
            return await self.connection.fetchrow(query, *args, timeout=timeout)

    async def fetchval(
        self,
        query: str,
        *args: Any,
        column: int = 0,
        timeout: float | None = None,
    ) -> Any:
        async with self.operation_lock:
            return await self.connection.fetchval(
                query,
                *args,
                column=column,
                timeout=timeout,
            )


# ── Lifecycle ──────────────────────────────────────────────────────────────


async def create_pool() -> asyncpg.Pool:
    """Create a reusable asyncpg pool with every session fixed to UTC.

    Safe to call multiple times; subsequent calls return the existing pool.
    SSL is configured from ``settings.pg_ssl_root_cert``.
    """
    global _pool

    if _pool is not None:
        return _pool

    ssl_context: ssl.SSLContext | None = None
    if settings.pg_ssl_root_cert.lower() not in {"", "disable", "none"}:
        ssl_context = ssl.create_default_context(cafile=settings.pg_ssl_root_cert)
        ssl_context.check_hostname = True
        ssl_context.verify_mode = ssl.CERT_REQUIRED

    logger.info(
        "creating postgres pool",
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
    )

    _pool = await asyncpg.create_pool(
        host=settings.pg_host,
        port=settings.pg_port,
        database=settings.pg_database,
        user=settings.pg_user,
        password=settings.pg_password,
        ssl=ssl_context,
        min_size=settings.pg_pool_min_size,
        max_size=settings.pg_pool_max_size,
        timeout=settings.pg_pool_acquire_timeout,
        command_timeout=settings.pg_pool_command_timeout,
        max_inactive_connection_lifetime=settings.pg_pool_max_inactive_lifetime,
        server_settings={"TimeZone": "UTC"},
    )

    logger.info("postgres pool created")
    return _pool


async def close_pool() -> None:
    """Close the connection pool gracefully."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("postgres pool closed")


@asynccontextmanager
async def bind_pool_connection(pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """Pin all ``get_pool`` calls in this async context to one owned session.

    The adapter serializes direct pool operations and nested ``acquire`` blocks.
    It is intended for short control-plane cycles that hold a session advisory
    lock and must not create a second database connection.
    """
    async with pool.acquire() as connection:
        pinned = _PinnedConnectionPool(cast(asyncpg.Connection, connection))
        token = _bound_pool.set(pinned)
        try:
            yield pinned.connection
        finally:
            _bound_pool.reset(token)


def get_pool() -> asyncpg.Pool:
    """Return the current connection pool.

    Raises :exc:`DBError` if the pool has not been initialised.
    """
    bound_pool = _bound_pool.get()
    if bound_pool is not None:
        return cast(asyncpg.Pool, bound_pool)
    if _pool is None:
        raise DBError("Database pool not initialised. Call create_pool() first.")
    return _pool
