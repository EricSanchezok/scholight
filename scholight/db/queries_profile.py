"""Scholight product-membership and block-state queries."""

from __future__ import annotations

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool

logger = structlog.get_logger(__name__)


class ProductAccessBlockedError(DBError):
    """The shared identity is blocked in Scholight only."""


async def ensure_product_access(user_id: int) -> None:
    """Create first-use membership and reject a Scholight-local block."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            await connection.execute(
                "INSERT INTO scholight.user_profiles (user_id) VALUES ($1) "
                "ON CONFLICT (user_id) DO NOTHING",
                user_id,
            )
            status = await connection.fetchval(
                "SELECT status FROM scholight.user_profiles WHERE user_id = $1",
                user_id,
            )
    except asyncpg.PostgresError as exc:
        logger.error("product_profile_access_failed", error_type=type(exc).__name__)
        raise DBError("Failed to verify Scholight product access") from exc
    if status == "blocked":
        raise ProductAccessBlockedError("Scholight access is blocked")
    if status != "active":
        raise DBError("Invalid Scholight product profile status")


__all__ = ["ProductAccessBlockedError", "ensure_product_access"]
