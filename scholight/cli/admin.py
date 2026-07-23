"""Scholight quota-administrator lifecycle commands."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import uuid4

import click
from pydantic import EmailStr, TypeAdapter, ValidationError

from scholight.db.client import DBError
from scholight.db.queries_admin import (
    AdminTargetNotFoundError,
    LastAdminError,
    TargetUserInactiveError,
)

_EMAIL_ADAPTER = TypeAdapter(EmailStr)


def _email(
    _context: click.Context,
    _parameter: click.Parameter,
    value: str,
) -> str:
    try:
        return str(_EMAIL_ADAPTER.validate_python(value))
    except ValidationError as exc:
        raise click.BadParameter("must be a complete email address") from exc


async def _run_admin_change(
    operation: Callable[..., object],
    email: str,
) -> bool:
    from scholight.db.client import close_pool, create_pool

    await create_pool()
    try:
        result = await operation(email, event_id=uuid4())  # type: ignore[misc]
        return bool(result)
    finally:
        await close_pool()


def _execute(operation_name: str, email: str) -> bool:
    from scholight.db.queries_admin import grant_quota_admin, revoke_quota_admin

    operation = grant_quota_admin if operation_name == "grant" else revoke_quota_admin
    try:
        return asyncio.run(_run_admin_change(operation, email))
    except AdminTargetNotFoundError as exc:
        raise click.ClickException("No user exists with that exact email address.") from exc
    except TargetUserInactiveError as exc:
        raise click.ClickException("The user must be active and verified.") from exc
    except LastAdminError as exc:
        raise click.ClickException("Cannot revoke the last active administrator.") from exc
    except DBError as exc:
        raise click.ClickException("Scholight administration service is unavailable.") from exc


@click.group("admin")
def admin_group() -> None:
    """Manage the small Scholight quota-administrator set."""


@admin_group.command()
@click.option("--email", required=True, callback=_email, help="Exact verified user email.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
def grant(email: str, yes: bool) -> None:
    """Grant Scholight quota administration."""
    if not yes:
        click.confirm(f"Grant quota administration to {email}?", abort=True)
    changed = _execute("grant", email)
    if changed:
        click.echo(f"Granted quota administration to {email}.")
    else:
        click.echo(f"{email} is already a quota administrator.")


@admin_group.command()
@click.option("--email", required=True, callback=_email, help="Exact administrator email.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
def revoke(email: str, yes: bool) -> None:
    """Revoke Scholight quota administration."""
    if not yes:
        click.confirm(f"Revoke quota administration from {email}?", abort=True)
    changed = _execute("revoke", email)
    if changed:
        click.echo(f"Revoked quota administration from {email}.")
    else:
        click.echo(f"{email} is not a quota administrator.")


__all__ = ["admin_group"]
