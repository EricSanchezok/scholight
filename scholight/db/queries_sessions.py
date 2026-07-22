"""Owner-scoped operations over cloud-auth refresh-token families."""

from __future__ import annotations

from typing import TYPE_CHECKING

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool

if TYPE_CHECKING:
    from scholight.api.sessions import SessionRecord

logger = structlog.get_logger(__name__)


async def query_sessions(user_id: int) -> list[SessionRecord]:
    """Return one session record per refresh-token family."""
    from scholight.api.sessions import SessionRecord

    try:
        rows = await get_pool().fetch(
            "SELECT family_id AS id, min(issued_at) AS created_at, "
            "max(COALESCE(last_seen_at, issued_at)) AS last_seen_at, "
            "max(expires_at) AS expires_at, "
            "(array_agg(user_agent ORDER BY issued_at DESC) "
            "FILTER (WHERE user_agent IS NOT NULL))[1] AS user_agent, "
            "CASE WHEN bool_or(revoked_at IS NULL AND expires_at > now()) "
            "THEN NULL ELSE max(revoked_at) END AS revoked_at "
            "FROM auth.refresh_tokens WHERE user_id = $1 "
            "GROUP BY family_id "
            "HAVING bool_or(revoked_at IS NULL AND expires_at > now()) "
            "ORDER BY created_at DESC",
            user_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("session_list_failed", error_type=type(exc).__name__)
        raise DBError("Failed to list sessions") from exc
    return [SessionRecord.model_validate(dict(row)) for row in rows]


async def register_session_metadata(
    *, user_id: int, session_id: int, user_agent: str | None
) -> None:
    """Attach metadata to the current active token in a newly issued/rotated family."""
    normalized_agent = user_agent[:512] if user_agent else None
    try:
        await get_pool().execute(
            "UPDATE auth.refresh_tokens SET user_agent = $3, last_seen_at = now() "
            "WHERE user_id = $1 AND family_id = $2 AND revoked_at IS NULL",
            user_id,
            session_id,
            normalized_agent,
        )
    except asyncpg.PostgresError as exc:
        logger.warning("session_metadata_write_failed", error_type=type(exc).__name__)
        raise DBError("Failed to record session metadata") from exc


async def touch_session(user_id: int, session_id: int) -> bool:
    """Validate one active family and update activity at most every five minutes."""
    try:
        pool = get_pool()
        active = await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM auth.refresh_tokens "
            "WHERE user_id = $1 AND family_id = $2 "
            "AND revoked_at IS NULL AND expires_at > now())",
            user_id,
            session_id,
        )
        if not active:
            return False
        await pool.execute(
            "UPDATE auth.refresh_tokens SET last_seen_at = now() "
            "WHERE user_id = $1 AND family_id = $2 AND revoked_at IS NULL "
            "AND (last_seen_at IS NULL OR last_seen_at < now() - interval '5 minutes')",
            user_id,
            session_id,
        )
    except (asyncpg.PostgresError, DBError) as exc:
        logger.warning("session_touch_failed", error_type=type(exc).__name__)
        return True
    return True


async def revoke_session(*, user_id: int, session_id: int) -> bool:
    try:
        async with get_pool().acquire() as connection:
            result = await connection.execute(
                "UPDATE auth.refresh_tokens SET revoked_at = COALESCE(revoked_at, now()) "
                "WHERE user_id = $1 AND family_id = $2 AND revoked_at IS NULL",
                user_id,
                session_id,
            )
    except asyncpg.PostgresError as exc:
        logger.error("session_revoke_failed", error_type=type(exc).__name__)
        raise DBError("Failed to revoke session") from exc
    return str(result) != "UPDATE 0"


async def revoke_other_sessions(*, user_id: int, current_session_id: int) -> int:
    try:
        result = await get_pool().execute(
            "UPDATE auth.refresh_tokens SET revoked_at = COALESCE(revoked_at, now()) "
            "WHERE user_id = $1 AND family_id <> $2 AND revoked_at IS NULL",
            user_id,
            current_session_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("other_sessions_revoke_failed", error_type=type(exc).__name__)
        raise DBError("Failed to revoke other sessions") from exc
    return int(str(result).rsplit(" ", 1)[-1])


__all__ = [
    "query_sessions",
    "register_session_metadata",
    "revoke_other_sessions",
    "revoke_session",
    "touch_session",
]
