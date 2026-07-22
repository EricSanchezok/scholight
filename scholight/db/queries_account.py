"""Transactional product-data cleanup and irreversible account anonymization."""

from __future__ import annotations

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool

logger = structlog.get_logger(__name__)


async def delete_user_account(user_id: int, *, replacement_password_hash: str) -> None:
    """Disable one account and remove its Scholight product data atomically."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            await connection.execute(
                "UPDATE auth.refresh_tokens SET revoked_at = COALESCE(revoked_at, now()) "
                "WHERE user_id = $1",
                user_id,
            )
            await connection.execute(
                "UPDATE public.access_keys SET revoked_at = COALESCE(revoked_at, now()) "
                "WHERE user_id = $1",
                user_id,
            )
            await connection.execute(
                "DELETE FROM public.search_history WHERE user_id = $1",
                user_id,
            )
            await connection.execute(
                "DELETE FROM public.usage_events WHERE user_id = $1",
                user_id,
            )
            await connection.execute(
                "DELETE FROM auth.daily_usage WHERE user_id = $1",
                user_id,
            )
            await connection.execute(
                "DELETE FROM auth.user_quotas WHERE user_id = $1",
                user_id,
            )
            result = await connection.execute(
                "UPDATE auth.users SET "
                "email = 'deleted+' || id::TEXT || '@deleted.invalid', "
                "display_name = NULL, password_hash = $2, status = 'disabled', "
                "email_verified_at = NULL, email_verify_token = NULL, "
                "email_verify_expires_at = NULL, password_reset_token = NULL, "
                "password_reset_expires_at = NULL, locked_until = NULL, "
                "deleted_at = now(), updated_at = now() WHERE id = $1",
                user_id,
                replacement_password_hash,
            )
            if str(result) != "UPDATE 1":
                raise DBError("Account not found")
    except DBError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("account_delete_failed", error_type=type(exc).__name__)
        raise DBError("Failed to delete account") from exc


__all__ = ["delete_user_account"]
