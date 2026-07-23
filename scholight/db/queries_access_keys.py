"""Owner-scoped PostgreSQL operations for Scholight personal access keys."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool

if TYPE_CHECKING:
    from scholight.api.access_keys import AccessKeyRecord

logger = structlog.get_logger(__name__)
_ACCESS_KEY_LIMIT = 10
_ADVISORY_NAMESPACE = 1_397_231_025


class AccessKeyLimitReachedError(DBError):
    """The owner already has the maximum number of active keys."""


def _record_from_row(row: asyncpg.Record) -> AccessKeyRecord:
    from scholight.api.access_keys import AccessKeyRecord

    raw = dict(row)
    raw["scopes"] = tuple(raw["scopes"])
    return AccessKeyRecord.model_validate(raw)


async def get_access_key_by_prefix(prefix: str) -> AccessKeyRecord | None:
    """Fetch one candidate by its public lookup prefix."""
    try:
        row = await get_pool().fetchrow(
            "SELECT id, user_id, name, key_prefix, key_last4, key_digest, scopes, "
            "created_at, last_used_at, expires_at, revoked_at "
            "FROM scholight.access_keys WHERE key_prefix = $1",
            prefix,
        )
    except asyncpg.PostgresError as exc:
        logger.error("access_key_lookup_failed", error_type=type(exc).__name__)
        raise DBError("Failed to resolve access key") from exc
    return _record_from_row(row) if row is not None else None


async def insert_access_key(record: AccessKeyRecord) -> AccessKeyRecord:
    """Insert a key while atomically enforcing the per-user active-key limit."""
    pool = get_pool()
    try:
        async with pool.acquire() as connection, connection.transaction():
            await connection.execute(
                "SELECT pg_advisory_xact_lock($1, $2)",
                _ADVISORY_NAMESPACE,
                record.user_id,
            )
            active_count = await connection.fetchval(
                "SELECT count(*) FROM scholight.access_keys WHERE user_id = $1 "
                "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())",
                record.user_id,
            )
            if int(active_count) >= _ACCESS_KEY_LIMIT:
                raise AccessKeyLimitReachedError("Active access-key limit reached")
            row = await connection.fetchrow(
                "INSERT INTO scholight.access_keys "
                "(id, user_id, name, key_prefix, key_last4, key_digest, scopes, expires_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) "
                "RETURNING id, user_id, name, key_prefix, key_last4, key_digest, scopes, "
                "created_at, last_used_at, expires_at, revoked_at",
                record.id,
                record.user_id,
                record.name,
                record.key_prefix,
                record.key_last4,
                record.key_digest,
                list(record.scopes),
                record.expires_at,
            )
    except AccessKeyLimitReachedError:
        raise
    except asyncpg.PostgresError as exc:
        logger.error("access_key_insert_failed", error_type=type(exc).__name__)
        raise DBError("Failed to create access key") from exc
    return _record_from_row(row)


async def list_access_keys(user_id: int) -> list[AccessKeyRecord]:
    """List only keys owned by the authenticated user."""
    try:
        rows = await get_pool().fetch(
            "SELECT id, user_id, name, key_prefix, key_last4, key_digest, scopes, "
            "created_at, last_used_at, expires_at, revoked_at "
            "FROM scholight.access_keys WHERE user_id = $1 ORDER BY created_at DESC, id DESC",
            user_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("access_key_list_failed", error_type=type(exc).__name__)
        raise DBError("Failed to list access keys") from exc
    return [_record_from_row(row) for row in rows]


async def update_access_key(
    key_id: UUID,
    user_id: int,
    *,
    name: str,
    expires_at: object,
) -> AccessKeyRecord | None:
    """Update mutable metadata using both key and owner identity."""
    try:
        row = await get_pool().fetchrow(
            "UPDATE scholight.access_keys SET name = $3, expires_at = $4 "
            "WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL "
            "RETURNING id, user_id, name, key_prefix, key_last4, key_digest, scopes, "
            "created_at, last_used_at, expires_at, revoked_at",
            key_id,
            user_id,
            name,
            expires_at,
        )
    except asyncpg.PostgresError as exc:
        logger.error("access_key_update_failed", error_type=type(exc).__name__)
        raise DBError("Failed to update access key") from exc
    return _record_from_row(row) if row is not None else None


async def revoke_access_key(key_id: UUID, user_id: int) -> bool:
    """Immediately revoke one owner-scoped key."""
    try:
        async with get_pool().acquire() as connection:
            result = await connection.execute(
                "UPDATE scholight.access_keys SET revoked_at = now() "
                "WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL",
                key_id,
                user_id,
            )
    except asyncpg.PostgresError as exc:
        logger.error("access_key_revoke_failed", error_type=type(exc).__name__)
        raise DBError("Failed to revoke access key") from exc
    return str(result) == "UPDATE 1"


async def touch_access_key_last_used(key_id: UUID, user_id: int) -> None:
    """Update last-used metadata at most once every five minutes."""
    try:
        await get_pool().execute(
            "UPDATE scholight.access_keys SET last_used_at = now() "
            "WHERE id = $1 AND user_id = $2 AND revoked_at IS NULL "
            "AND (last_used_at IS NULL OR last_used_at < now() - interval '5 minutes')",
            key_id,
            user_id,
        )
    except asyncpg.PostgresError as exc:
        logger.warning("access_key_touch_failed", error_type=type(exc).__name__)


__all__ = [
    "AccessKeyLimitReachedError",
    "get_access_key_by_prefix",
    "insert_access_key",
    "list_access_keys",
    "revoke_access_key",
    "touch_access_key_last_used",
    "update_access_key",
]
