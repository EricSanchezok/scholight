"""Quota administrator lifecycle CLI tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from scholight.cli import cli
from scholight.db.queries_admin import LastAdminError, TargetUserInactiveError


def test_admin_grant_requires_confirmation_and_closes_pool() -> None:
    with (
        patch("scholight.db.client.create_pool", AsyncMock()) as create_pool,
        patch("scholight.db.client.close_pool", AsyncMock()) as close_pool,
        patch(
            "scholight.db.queries_admin.grant_quota_admin",
            AsyncMock(return_value=True),
        ) as grant,
    ):
        result = CliRunner().invoke(
            cli,
            ["admin", "grant", "--email", "admin@example.com"],
            input="y\n",
        )

    assert result.exit_code == 0
    create_pool.assert_awaited_once_with()
    grant.assert_awaited_once()
    close_pool.assert_awaited_once_with()


def test_admin_grant_yes_skips_confirmation() -> None:
    with (
        patch("scholight.db.client.create_pool", AsyncMock()),
        patch("scholight.db.client.close_pool", AsyncMock()),
        patch(
            "scholight.db.queries_admin.grant_quota_admin",
            AsyncMock(return_value=False),
        ),
    ):
        result = CliRunner().invoke(
            cli,
            ["admin", "grant", "--email", "admin@example.com", "--yes"],
        )

    assert result.exit_code == 0
    assert "already" in result.output.lower()


def test_admin_grant_rejects_non_email_before_database_access() -> None:
    with patch("scholight.db.client.create_pool", AsyncMock()) as create_pool:
        result = CliRunner().invoke(
            cli,
            ["admin", "grant", "--email", "not-an-email", "--yes"],
        )

    assert result.exit_code == 2
    create_pool.assert_not_awaited()


def test_admin_grant_reports_inactive_user_without_sql_details() -> None:
    with (
        patch("scholight.db.client.create_pool", AsyncMock()),
        patch("scholight.db.client.close_pool", AsyncMock()),
        patch(
            "scholight.db.queries_admin.grant_quota_admin",
            AsyncMock(side_effect=TargetUserInactiveError("private SQL detail")),
        ),
    ):
        result = CliRunner().invoke(
            cli,
            ["admin", "grant", "--email", "admin@example.com", "--yes"],
        )

    assert result.exit_code == 1
    assert "active and verified" in result.output
    assert "private SQL detail" not in result.output


def test_admin_revoke_refuses_last_admin_and_closes_pool() -> None:
    with (
        patch("scholight.db.client.create_pool", AsyncMock()),
        patch("scholight.db.client.close_pool", AsyncMock()) as close_pool,
        patch(
            "scholight.db.queries_admin.revoke_quota_admin",
            AsyncMock(side_effect=LastAdminError("last")),
        ),
    ):
        result = CliRunner().invoke(
            cli,
            ["admin", "revoke", "--email", "admin@example.com", "--yes"],
        )

    assert result.exit_code == 1
    assert "last active administrator" in result.output.lower()
    close_pool.assert_awaited_once_with()
