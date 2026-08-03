"""JWT-only personal access-key management routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Response, status
from sanchezcloud_identity.models.user import UserRecord

from scholight.api.access_keys import issue_access_key
from scholight.api.deps import get_current_user
from scholight.api.http_errors import http_error
from scholight.api.models.access_key import (
    AccessKeyResponse,
    CreateAccessKeyRequest,
    CreatedAccessKeyResponse,
    UpdateAccessKeyRequest,
)
from scholight.db.client import DBError
from scholight.db.queries_access_keys import (
    AccessKeyLimitReachedError,
    list_access_keys,
    revoke_access_key,
    update_access_key,
)

router = APIRouter()


@router.post("", response_model=CreatedAccessKeyResponse, status_code=status.HTTP_201_CREATED)
async def create_key(
    body: CreateAccessKeyRequest,
    current_user: UserRecord = Depends(get_current_user),
) -> CreatedAccessKeyResponse:
    try:
        record, plaintext = await issue_access_key(
            user_id=current_user.id,
            name=body.name,
            expires_at=body.expires_at,
        )
    except AccessKeyLimitReachedError as exc:
        raise http_error(
            409,
            code="access_key_limit_reached",
            message=(
                "You already have the maximum number of active access keys. "
                "Revoke one before creating another."
            ),
            retryable=False,
            retry_after=None,
        ) from exc
    except DBError as exc:
        raise http_error(
            503,
            code="access_key_service_unavailable",
            message="Access key management is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    public = AccessKeyResponse.from_record(record)
    return CreatedAccessKeyResponse(**public.model_dump(), key=plaintext)


@router.get("", response_model=list[AccessKeyResponse])
async def get_keys(
    current_user: UserRecord = Depends(get_current_user),
) -> list[AccessKeyResponse]:
    try:
        records = await list_access_keys(current_user.id)
    except DBError as exc:
        raise http_error(
            503,
            code="access_key_service_unavailable",
            message="Access key management is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    return [AccessKeyResponse.from_record(record) for record in records]


@router.patch("/{key_id}", response_model=AccessKeyResponse)
async def patch_key(
    key_id: UUID,
    body: UpdateAccessKeyRequest,
    current_user: UserRecord = Depends(get_current_user),
) -> AccessKeyResponse:
    try:
        keys = await list_access_keys(current_user.id)
    except DBError as exc:
        raise http_error(
            503,
            code="access_key_service_unavailable",
            message="Access key management is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    existing = next((key for key in keys if key.id == key_id), None)
    if existing is None or existing.revoked_at is not None:
        raise http_error(
            404,
            code="access_key_not_found",
            message="This access key does not exist or has already been revoked.",
            retryable=False,
            retry_after=None,
        )
    name = body.name
    if "name" not in body.model_fields_set or name is None:
        name = existing.name
    expires_at = body.expires_at if "expires_at" in body.model_fields_set else existing.expires_at
    try:
        updated = await update_access_key(
            key_id,
            current_user.id,
            name=name,
            expires_at=expires_at,
        )
    except DBError as exc:
        raise http_error(
            503,
            code="access_key_service_unavailable",
            message="Access key management is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    if updated is None:
        raise http_error(
            404,
            code="access_key_not_found",
            message="This access key does not exist or has already been revoked.",
            retryable=False,
            retry_after=None,
        )
    return AccessKeyResponse.from_record(updated)


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_key(
    key_id: UUID,
    current_user: UserRecord = Depends(get_current_user),
) -> Response:
    try:
        revoked = await revoke_access_key(key_id, current_user.id)
    except DBError as exc:
        raise http_error(
            503,
            code="access_key_service_unavailable",
            message="Access key management is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    if not revoked:
        raise http_error(
            404,
            code="access_key_not_found",
            message="This access key does not exist or has already been revoked.",
            retryable=False,
            retry_after=None,
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
