"""Read-only presentation adapter for the shared SanchezCloud avatar."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from fastapi import APIRouter, Depends
from sanchezcloud_identity.exceptions import AvatarNotFoundError, AvatarStorageError, DBError
from sanchezcloud_identity.models.avatar import AvatarView

from scholight.api.http_errors import http_error

if TYPE_CHECKING:
    from sanchezcloud_identity.models.user import UserRecord


class AvatarReader(Protocol):
    """Narrow SDK boundary needed by the Scholight presentation layer."""

    async def get(self, user_id: int) -> AvatarView: ...


def get_avatar_read_router(
    *,
    avatar_reader: AvatarReader | None,
    get_current_user: Callable[..., object],
) -> APIRouter:
    """Expose avatar reads without granting Scholight avatar mutation routes."""
    router = APIRouter()

    @router.get("/avatar", response_model=AvatarView)
    async def get_avatar(
        current_user: UserRecord = Depends(get_current_user),
    ) -> AvatarView:
        if avatar_reader is None:
            raise http_error(
                404,
                code="avatar_not_found",
                message="No profile photo is available.",
                retryable=False,
                retry_after=None,
            )
        try:
            return await avatar_reader.get(current_user.id)
        except AvatarNotFoundError as exc:
            raise http_error(
                404,
                code="avatar_not_found",
                message="No profile photo is available.",
                retryable=False,
                retry_after=None,
            ) from exc
        except (AvatarStorageError, DBError) as exc:
            raise http_error(
                503,
                code="avatar_service_unavailable",
                message="The profile photo is temporarily unavailable.",
                retryable=True,
                retry_after=5,
            ) from exc

    return router


__all__ = ["AvatarReader", "get_avatar_read_router"]
