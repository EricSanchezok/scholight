"""Survey email notification outbox query contracts."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from scholight.db.queries_survey_notifications import (
    claim_email_notification,
    recover_expired_email_notifications,
)


@pytest.mark.asyncio
async def test_claim_uses_skip_locked_and_current_verified_account_email() -> None:
    connection = MagicMock()
    connection.fetchrow = AsyncMock(return_value=None)
    transaction = AsyncMock()
    connection.transaction.return_value.__aenter__ = transaction
    connection.transaction.return_value.__aexit__ = AsyncMock(return_value=None)
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=connection)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=None)

    with patch("scholight.db.queries_survey_notifications.get_pool", return_value=pool):
        notification = await claim_email_notification(worker_id=uuid4(), lease_seconds=120)

    assert notification is None
    sql = connection.fetchrow.await_args_list[0].args[0]
    assert "FOR UPDATE OF notifications SKIP LOCKED" in sql
    assert "JOIN auth.users" in sql
    assert "email_verified_at" in sql


@pytest.mark.asyncio
async def test_recovery_only_requeues_expired_running_notifications() -> None:
    pool = MagicMock()
    pool.execute = AsyncMock(return_value="UPDATE 2")

    with patch("scholight.db.queries_survey_notifications.get_pool", return_value=pool):
        count = await recover_expired_email_notifications()

    assert count == 2
    sql = pool.execute.await_args.args[0]
    assert "status = 'running' AND lease_expires_at <= now()" in sql
