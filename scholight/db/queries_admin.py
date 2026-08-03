"""Audited Scholight quota-administrator operations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

import asyncpg
import structlog

from scholight.db.client import DBError, get_pool

logger = structlog.get_logger(__name__)

AdminAction = Literal["quota_overrides_updated", "admin_granted", "admin_revoked"]
AdminActorType = Literal["user", "cli"]
_ADMIN_LIFECYCLE_LOCK_ID = 7_192_003_902


class AdminTargetNotFoundError(DBError):
    """The exact shared identity does not exist."""


class TargetUserInactiveError(DBError):
    """The target cannot receive active Scholight administration."""


class LastAdminError(DBError):
    """Revoking this administrator would leave no valid administrator."""


@dataclass(frozen=True, slots=True)
class AdminTarget:
    id: int
    email: str
    display_name: str | None
    account_status: str
    email_verified: bool


@dataclass(frozen=True, slots=True)
class AdminAuditEvent:
    event_id: UUID
    actor_type: AdminActorType
    actor_identifier: str
    target_user_id: int | None
    target_email: str
    action: AdminAction
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    created_at: datetime


def _target(row: asyncpg.Record | dict[str, Any]) -> AdminTarget:
    return AdminTarget(
        id=int(row["id"]),
        email=str(row["email"]),
        display_name=(str(row["display_name"]) if row.get("display_name") is not None else None),
        account_status=str(row["account_status"]),
        email_verified=bool(row["email_verified"]),
    )


def _row_value(row: asyncpg.Record | dict[str, Any], key: str) -> Any:
    return row.get(key)


def _json_state(value: Any) -> dict[str, Any]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise DBError("Invalid administration audit state")
    return decoded


async def is_scholight_admin(user_id: int) -> bool:
    """Check current Scholight administration state on every request."""
    try:
        value = await get_pool().fetchval(
            "SELECT EXISTS ("
            "SELECT 1 FROM scholight.user_profiles "
            "WHERE user_id = $1 AND status = 'active' AND is_admin IS TRUE"
            ")",
            user_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("scholight_admin_check_failed", error_type=type(exc).__name__)
        raise DBError("Failed to verify Scholight administrator permission") from exc
    return value is True


async def find_admin_target_by_email(email: str) -> AdminTarget | None:
    """Resolve one exact shared identity without offering user enumeration."""
    try:
        row = await get_pool().fetchrow(
            "SELECT users.id, users.email, users.display_name, "
            "users.status AS account_status, "
            "users.email_verified_at IS NOT NULL AS email_verified "
            "FROM auth.users AS users "
            "WHERE lower(users.email) = lower($1)",
            email,
        )
    except asyncpg.PostgresError as exc:
        logger.error("admin_target_lookup_failed", error_type=type(exc).__name__)
        raise DBError("Failed to look up quota administration target") from exc
    return _target(row) if row is not None else None


async def get_user_quota_overrides(user_id: int) -> dict[str, int | None]:
    """Return both optional product overrides without calculating effective quota."""
    try:
        rows = await get_pool().fetch(
            "SELECT strength, daily_limit FROM scholight.user_quota_overrides WHERE user_id = $1",
            user_id,
        )
    except asyncpg.PostgresError as exc:
        logger.error("admin_quota_overrides_read_failed", error_type=type(exc).__name__)
        raise DBError("Failed to read user quota overrides") from exc
    values: dict[str, int | None] = {"standard": None, "thorough": None, "survey": None}
    for row in rows:
        values[str(row["strength"])] = int(row["daily_limit"])
    return values


async def _insert_audit_event(
    connection: asyncpg.Connection,
    *,
    event_id: UUID,
    actor_type: AdminActorType,
    actor_user_id: int | None,
    actor_identifier: str,
    target_user_id: int,
    target_email: str,
    action: AdminAction,
    before_state: dict[str, Any],
    after_state: dict[str, Any],
) -> None:
    await connection.execute(
        "INSERT INTO scholight.admin_audit_events ("
        "event_id, actor_type, actor_user_id, actor_identifier, "
        "target_user_id, target_email, action, before_state, after_state"
        ") VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, $9::jsonb)",
        event_id,
        actor_type,
        actor_user_id,
        actor_identifier,
        target_user_id,
        target_email,
        action,
        json.dumps(before_state, separators=(",", ":"), sort_keys=True),
        json.dumps(after_state, separators=(",", ":"), sort_keys=True),
    )


async def _locked_target_by_id(
    connection: asyncpg.Connection, user_id: int
) -> asyncpg.Record | None:
    return await connection.fetchrow(
        "SELECT users.id, users.email, users.status AS account_status, "
        "users.email_verified_at IS NOT NULL AS email_verified, "
        "profiles.status AS product_status "
        "FROM auth.users AS users "
        "LEFT JOIN scholight.user_profiles AS profiles ON profiles.user_id = users.id "
        "WHERE users.id = $1 FOR UPDATE OF users",
        user_id,
    )


def _require_active_target(
    row: asyncpg.Record | dict[str, Any] | None,
) -> asyncpg.Record | dict[str, Any]:
    if row is None:
        raise AdminTargetNotFoundError("Quota administration target not found")
    if (
        row["account_status"] != "active"
        or not bool(row["email_verified"])
        or _row_value(row, "product_status") == "blocked"
    ):
        raise TargetUserInactiveError("Quota administration target is not active")
    return row


async def update_user_quota_overrides(
    *,
    actor_user_id: int,
    actor_email: str,
    target_user_id: int,
    standard: int | None,
    thorough: int | None,
    survey: int | None,
    event_id: UUID,
) -> bool:
    """Atomically replace all overrides and append one immutable audit event."""
    requested = {"standard": standard, "thorough": thorough, "survey": survey}
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            row = _require_active_target(await _locked_target_by_id(connection, target_user_id))
            await connection.execute(
                "INSERT INTO scholight.user_profiles (user_id) VALUES ($1) "
                "ON CONFLICT (user_id) DO NOTHING",
                target_user_id,
            )
            rows = await connection.fetch(
                "SELECT strength, daily_limit FROM scholight.user_quota_overrides "
                "WHERE user_id = $1 FOR UPDATE",
                target_user_id,
            )
            before: dict[str, int | None] = {
                "standard": None,
                "thorough": None,
                "survey": None,
            }
            for override in rows:
                value = override["daily_limit"]
                before[str(override["strength"])] = int(value) if value is not None else None
            if before == requested:
                return False
            for strength, limit in requested.items():
                if limit is None:
                    await connection.execute(
                        "DELETE FROM scholight.user_quota_overrides "
                        "WHERE user_id = $1 AND strength = $2",
                        target_user_id,
                        strength,
                    )
                else:
                    await connection.execute(
                        "INSERT INTO scholight.user_quota_overrides "
                        "(user_id, strength, daily_limit, updated_at) "
                        "VALUES ($1, $2, $3, statement_timestamp()) "
                        "ON CONFLICT (user_id, strength) DO UPDATE SET "
                        "daily_limit = EXCLUDED.daily_limit, "
                        "updated_at = statement_timestamp()",
                        target_user_id,
                        strength,
                        limit,
                    )
            await _insert_audit_event(
                connection,
                event_id=event_id,
                actor_type="user",
                actor_user_id=actor_user_id,
                actor_identifier=actor_email,
                target_user_id=target_user_id,
                target_email=str(row["email"]),
                action="quota_overrides_updated",
                before_state=before,
                after_state=requested,
            )
    except (AdminTargetNotFoundError, TargetUserInactiveError):
        raise
    except asyncpg.PostgresError as exc:
        logger.error("admin_quota_update_failed", error_type=type(exc).__name__)
        raise DBError("Failed to update user quota overrides") from exc
    return True


async def list_admin_audit_events(limit: int) -> list[AdminAuditEvent]:
    """Return newest immutable administration events."""
    try:
        rows = await get_pool().fetch(
            "SELECT event_id, actor_type, actor_identifier, target_user_id, "
            "target_email, action, before_state, after_state, created_at "
            "FROM scholight.admin_audit_events "
            "ORDER BY created_at DESC, id DESC LIMIT $1",
            limit,
        )
    except asyncpg.PostgresError as exc:
        logger.error("admin_audit_list_failed", error_type=type(exc).__name__)
        raise DBError("Failed to list administration audit events") from exc
    return [
        AdminAuditEvent(
            event_id=UUID(str(row["event_id"])),
            actor_type=row["actor_type"],
            actor_identifier=str(row["actor_identifier"]),
            target_user_id=(
                int(row["target_user_id"]) if row["target_user_id"] is not None else None
            ),
            target_email=str(row["target_email"]),
            action=row["action"],
            before_state=_json_state(row["before_state"]),
            after_state=_json_state(row["after_state"]),
            created_at=row["created_at"],
        )
        for row in rows
    ]


async def _locked_target_by_email(
    connection: asyncpg.Connection, email: str
) -> asyncpg.Record | None:
    return await connection.fetchrow(
        "SELECT users.id, users.email, users.status AS account_status, "
        "users.email_verified_at IS NOT NULL AS email_verified, "
        "profiles.status AS product_status, COALESCE(profiles.is_admin, FALSE) AS is_admin "
        "FROM auth.users AS users "
        "LEFT JOIN scholight.user_profiles AS profiles ON profiles.user_id = users.id "
        "WHERE lower(users.email) = lower($1) FOR UPDATE OF users",
        email,
    )


async def grant_quota_admin(email: str, *, event_id: UUID) -> bool:
    """Grant product administration to one exact active, verified identity."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", _ADMIN_LIFECYCLE_LOCK_ID)
            row = _require_active_target(await _locked_target_by_email(connection, email))
            if bool(row["is_admin"]):
                return False
            await connection.execute(
                "INSERT INTO scholight.user_profiles (user_id, is_admin) VALUES ($1, TRUE) "
                "ON CONFLICT (user_id) DO UPDATE SET "
                "is_admin = TRUE, updated_at = statement_timestamp()",
                int(row["id"]),
            )
            await _insert_audit_event(
                connection,
                event_id=event_id,
                actor_type="cli",
                actor_user_id=None,
                actor_identifier="cli",
                target_user_id=int(row["id"]),
                target_email=str(row["email"]),
                action="admin_granted",
                before_state={"is_admin": False},
                after_state={"is_admin": True},
            )
    except (AdminTargetNotFoundError, TargetUserInactiveError):
        raise
    except asyncpg.PostgresError as exc:
        logger.error("quota_admin_grant_failed", error_type=type(exc).__name__)
        raise DBError("Failed to grant quota administrator") from exc
    return True


async def revoke_quota_admin(email: str, *, event_id: UUID) -> bool:
    """Revoke one product administrator while preserving a valid last admin."""
    try:
        async with get_pool().acquire() as connection, connection.transaction():
            await connection.execute("SELECT pg_advisory_xact_lock($1)", _ADMIN_LIFECYCLE_LOCK_ID)
            row = await _locked_target_by_email(connection, email)
            if row is None:
                raise AdminTargetNotFoundError("Quota administrator not found")
            if not bool(row["is_admin"]):
                return False
            active_admins = await connection.fetchval(
                "SELECT count(*) FROM scholight.user_profiles AS profiles "
                "JOIN auth.users AS users ON users.id = profiles.user_id "
                "WHERE profiles.is_admin IS TRUE AND profiles.status = 'active' "
                "AND users.status = 'active' AND users.email_verified_at IS NOT NULL"
            )
            target_is_valid = (
                row["account_status"] == "active"
                and bool(row["email_verified"])
                and row["product_status"] == "active"
            )
            if target_is_valid and int(active_admins or 0) <= 1:
                raise LastAdminError("Cannot revoke the last active quota administrator")
            await connection.execute(
                "UPDATE scholight.user_profiles SET is_admin = FALSE, "
                "updated_at = statement_timestamp() WHERE user_id = $1",
                int(row["id"]),
            )
            await _insert_audit_event(
                connection,
                event_id=event_id,
                actor_type="cli",
                actor_user_id=None,
                actor_identifier="cli",
                target_user_id=int(row["id"]),
                target_email=str(row["email"]),
                action="admin_revoked",
                before_state={"is_admin": True},
                after_state={"is_admin": False},
            )
    except (AdminTargetNotFoundError, LastAdminError):
        raise
    except asyncpg.PostgresError as exc:
        logger.error("quota_admin_revoke_failed", error_type=type(exc).__name__)
        raise DBError("Failed to revoke quota administrator") from exc
    return True


__all__ = [
    "AdminAuditEvent",
    "AdminTarget",
    "AdminTargetNotFoundError",
    "LastAdminError",
    "TargetUserInactiveError",
    "find_admin_target_by_email",
    "get_user_quota_overrides",
    "grant_quota_admin",
    "is_scholight_admin",
    "list_admin_audit_events",
    "revoke_quota_admin",
    "update_user_quota_overrides",
]
