"""PostgreSQL connection pool lifecycle — asyncpg pool manager with SSL."""

from __future__ import annotations

import ssl

import asyncpg
import structlog

from compass.config import settings

logger = structlog.get_logger(__name__)

# ── Custom exceptions ──────────────────────────────────────────────────────


class DBError(Exception):
    """Base exception for database-layer errors."""


# ── Module-level pool ──────────────────────────────────────────────────────

_pool: asyncpg.Pool | None = None


# ── Lifecycle ──────────────────────────────────────────────────────────────


async def create_pool() -> asyncpg.Pool:
    """Create and store the asyncpg connection pool.  Safe to call multiple
    times — subsequent calls return the existing pool.

    Configures SSL context from ``settings.pg_ssl_root_cert``.
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


def get_pool() -> asyncpg.Pool:
    """Return the current connection pool.

    Raises :exc:`DBError` if the pool has not been initialised.
    """
    if _pool is None:
        raise DBError("Database pool not initialised. Call create_pool() first.")
    return _pool
