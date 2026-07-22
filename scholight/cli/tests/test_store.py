"""Tests for PostgreSQL migration CLI wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from scholight.cli import cli


def test_store_migrate_runs_and_closes_pool() -> None:
    pool = object()

    with (
        patch("scholight.db.client.create_pool", AsyncMock(return_value=pool)) as create_pool,
        patch("scholight.db.client.close_pool", AsyncMock()) as close_pool,
        patch("scholight.db.migrate_all.run_all_migrations", AsyncMock()) as run_migrations,
    ):
        result = CliRunner().invoke(cli, ["store", "migrate"])

    assert result.exit_code == 0
    create_pool.assert_awaited_once_with()
    run_migrations.assert_awaited_once_with(pool)
    close_pool.assert_awaited_once_with()


def test_store_migrate_closes_pool_after_failure() -> None:
    pool = object()

    with (
        patch("scholight.db.client.create_pool", AsyncMock(return_value=pool)),
        patch("scholight.db.client.close_pool", AsyncMock()) as close_pool,
        patch(
            "scholight.db.migrate_all.run_all_migrations",
            AsyncMock(side_effect=RuntimeError("migration failed")),
        ),
    ):
        result = CliRunner().invoke(cli, ["store", "migrate"])

    assert result.exit_code == 1
    close_pool.assert_awaited_once_with()
