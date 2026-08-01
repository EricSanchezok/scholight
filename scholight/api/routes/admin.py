"""JWT-only Scholight quota administration routes."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, EmailStr, Field
from sanchezcloud_identity.models.user import UserRecord

from scholight.api.deps import get_current_user, get_scholight_admin
from scholight.api.http_errors import http_error
from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.queries_admin import (
    AdminAuditEvent,
    AdminTarget,
    AdminTargetNotFoundError,
    TargetUserInactiveError,
    find_admin_target_by_email,
    get_user_quota_overrides,
    is_scholight_admin,
    list_admin_audit_events,
    update_user_quota_overrides,
)
from scholight.db.queries_quota import get_user_quota_status

router = APIRouter()


class AdminCapabilitiesResponse(BaseModel):
    can_manage_quotas: bool
    can_view_operations: bool
    can_view_analytics: bool


class AdminUserResponse(BaseModel):
    id: int
    email: EmailStr
    display_name: str | None
    account_status: str


class AdminQuotaResponse(BaseModel):
    default_limit: int = Field(ge=0)
    override_limit: int | None = Field(ge=0, le=1_000_000)
    effective_limit: int = Field(ge=0)
    used: int = Field(ge=0)
    remaining: int = Field(ge=0)


class AdminQuotasResponse(BaseModel):
    standard: AdminQuotaResponse
    thorough: AdminQuotaResponse


class AdminUserLookupResponse(BaseModel):
    user: AdminUserResponse
    quotas: AdminQuotasResponse


class QuotaOverrideRequest(BaseModel):
    standard: int | None = Field(ge=0, le=1_000_000)
    thorough: int | None = Field(ge=0, le=1_000_000)


class QuotaOverrideUpdateResponse(BaseModel):
    changed: bool


class AdminAuditEventResponse(BaseModel):
    event_id: UUID
    actor_type: str
    actor_identifier: str
    target_user_id: int | None
    target_email: EmailStr
    action: str
    before_state: dict[str, Any]
    after_state: dict[str, Any]
    created_at: datetime


async def _lookup(target: AdminTarget) -> AdminUserLookupResponse:
    overrides = await get_user_quota_overrides(target.id)
    statuses = await get_user_quota_status(
        target.id,
        standard_default_limit=settings.authenticated_standard_daily_limit,
        thorough_default_limit=settings.authenticated_thorough_daily_limit,
    )
    by_strength = {status.strength: status for status in statuses}
    defaults = {
        "standard": settings.authenticated_standard_daily_limit,
        "thorough": settings.authenticated_thorough_daily_limit,
    }
    quotas: dict[str, AdminQuotaResponse] = {}
    for strength in ("standard", "thorough"):
        status = by_strength[strength]
        quotas[strength] = AdminQuotaResponse(
            default_limit=defaults[strength],
            override_limit=overrides[strength],
            effective_limit=status.daily_limit,
            used=status.used,
            remaining=status.remaining,
        )
    return AdminUserLookupResponse(
        user=AdminUserResponse(
            id=target.id,
            email=target.email,
            display_name=target.display_name,
            account_status=target.account_status,
        ),
        quotas=AdminQuotasResponse(
            standard=quotas["standard"],
            thorough=quotas["thorough"],
        ),
    )


@router.get("/capabilities", response_model=AdminCapabilitiesResponse)
async def capabilities(
    current_user: UserRecord = Depends(get_current_user),
) -> AdminCapabilitiesResponse:
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
    return AdminCapabilitiesResponse(
        can_manage_quotas=permitted,
        can_view_operations=permitted,
        can_view_analytics=permitted,
    )


@router.get("/users/lookup", response_model=AdminUserLookupResponse)
async def lookup_user(
    email: Annotated[EmailStr, Query()],
    _admin: UserRecord = Depends(get_scholight_admin),
) -> AdminUserLookupResponse:
    try:
        target = await find_admin_target_by_email(str(email))
        if target is None:
            raise http_error(
                404,
                code="user_not_found",
                message="No Scholight user exists with that exact email address.",
                retryable=False,
                retry_after=None,
            )
        return await _lookup(target)
    except HTTPException:
        raise
    except DBError as exc:
        raise http_error(
            503,
            code="admin_service_unavailable",
            message="Administration service is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc


@router.put(
    "/users/{user_id}/quota-overrides",
    response_model=QuotaOverrideUpdateResponse,
)
async def update_quota_overrides(
    user_id: int,
    body: QuotaOverrideRequest,
    admin: UserRecord = Depends(get_scholight_admin),
) -> QuotaOverrideUpdateResponse:
    try:
        changed = await update_user_quota_overrides(
            actor_user_id=admin.id,
            actor_email=str(admin.email),
            target_user_id=user_id,
            standard=body.standard,
            thorough=body.thorough,
            event_id=uuid4(),
        )
    except AdminTargetNotFoundError as exc:
        raise http_error(
            404,
            code="user_not_found",
            message="The Scholight user no longer exists.",
            retryable=False,
            retry_after=None,
        ) from exc
    except TargetUserInactiveError as exc:
        raise http_error(
            409,
            code="user_not_active",
            message="Quota overrides can only be changed for an active Scholight user.",
            retryable=False,
            retry_after=None,
        ) from exc
    except DBError as exc:
        raise http_error(
            503,
            code="admin_service_unavailable",
            message="Administration service is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    return QuotaOverrideUpdateResponse(changed=changed)


@router.get("/audit-events", response_model=list[AdminAuditEventResponse])
async def audit_events(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    _admin: UserRecord = Depends(get_scholight_admin),
) -> list[AdminAuditEventResponse]:
    try:
        events = await list_admin_audit_events(limit)
    except DBError as exc:
        raise http_error(
            503,
            code="admin_service_unavailable",
            message="Administration service is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    return [_audit_response(event) for event in events]


def _audit_response(event: AdminAuditEvent) -> AdminAuditEventResponse:
    return AdminAuditEventResponse(
        event_id=event.event_id,
        actor_type=event.actor_type,
        actor_identifier=event.actor_identifier,
        target_user_id=event.target_user_id,
        target_email=event.target_email,
        action=event.action,
        before_state=event.before_state,
        after_state=event.after_state,
        created_at=event.created_at,
    )


__all__ = ["router"]
