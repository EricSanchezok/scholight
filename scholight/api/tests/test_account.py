"""Transactional account deletion behavior tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from cloud_auth.models.user import UserRecord
from fastapi import HTTPException

from scholight.api.models.account import DeleteAccountRequest
from scholight.api.routes.account import delete_account


@pytest.mark.asyncio
async def test_account_delete_rejects_incorrect_confirmation(active_user: UserRecord) -> None:
    body = DeleteAccountRequest(password="correct-password", confirmation="delete")

    with pytest.raises(HTTPException) as exc_info:
        await delete_account(body, active_user)

    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "account_delete_confirmation_invalid"


@pytest.mark.asyncio
async def test_account_delete_rejects_wrong_current_password(active_user: UserRecord) -> None:
    body = DeleteAccountRequest(password="wrong-password", confirmation="DELETE")

    with (
        patch("scholight.api.routes.account.verify_password", AsyncMock(return_value=False)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await delete_account(body, active_user)

    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["code"] == "current_password_invalid"


@pytest.mark.asyncio
async def test_account_delete_calls_single_transactional_cleanup(active_user: UserRecord) -> None:
    body = DeleteAccountRequest(password="correct-password", confirmation="DELETE")

    with (
        patch("scholight.api.routes.account.verify_password", AsyncMock(return_value=True)),
        patch("scholight.api.routes.account.hash_password", AsyncMock(return_value="new-hash")),
        patch("scholight.api.routes.account.delete_user_account", AsyncMock()) as cleanup,
    ):
        response = await delete_account(body, active_user)

    cleanup.assert_awaited_once_with(active_user.id, replacement_password_hash="new-hash")
    assert response.status_code == 204
