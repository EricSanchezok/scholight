"""JWT-only destructive account lifecycle route."""

from __future__ import annotations

import secrets

from cloud_auth.models.user import UserRecord
from cloud_auth.password import hash_password, verify_password
from fastapi import APIRouter, Depends, HTTPException, Response, status

from scholight.api.deps import get_current_user
from scholight.api.models.account import DeleteAccountRequest
from scholight.db.client import DBError
from scholight.db.queries_account import delete_user_account

router = APIRouter()


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": False},
    )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    body: DeleteAccountRequest,
    current_user: UserRecord = Depends(get_current_user),
) -> Response:
    if body.confirmation != "DELETE":
        raise _error(
            400,
            "account_delete_confirmation_invalid",
            "Type DELETE to confirm account deletion.",
        )
    if not await verify_password(body.password, current_user.password_hash):
        raise _error(400, "current_password_invalid", "Current password is invalid.")
    replacement_hash = await hash_password(secrets.token_urlsafe(48))
    try:
        await delete_user_account(
            current_user.id,
            replacement_password_hash=replacement_hash,
        )
    except DBError as exc:
        raise _error(503, "account_delete_failed", "Account deletion failed.") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


__all__ = ["router"]
