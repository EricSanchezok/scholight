"""Scholight Survey worker operations."""

from __future__ import annotations

import asyncio
import json

import click

from scholight.config import (
    validate_survey_draft_worker_settings,
    validate_survey_worker_settings,
)
from scholight.db.client import close_pool, create_pool
from scholight.db.queries_survey import get_survey_job_counts
from scholight.logging import configure_logging
from scholight.survey.worker import RCM_VERSION, serve_survey_worker


@click.group("survey")
def survey_group() -> None:
    """Run and inspect durable Scholight Survey jobs."""


@survey_group.command("serve-worker")
def serve_worker() -> None:
    """Run the concurrent Survey execution and archive supervisor."""
    configure_logging()
    try:
        validate_survey_worker_settings()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    async def _run() -> None:
        await create_pool()
        try:
            await serve_survey_worker()
        finally:
            await close_pool()

    asyncio.run(_run())


@survey_group.command("serve-draft-worker")
def serve_draft_worker() -> None:
    """Run the independent concurrent Survey Draft supervisor."""
    configure_logging()
    try:
        validate_survey_draft_worker_settings()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    async def _run() -> None:
        from scholight.survey.draft_worker import serve_survey_draft_worker

        await create_pool()
        try:
            await serve_survey_draft_worker()
        finally:
            await close_pool()

    asyncio.run(_run())


@survey_group.command("status")
@click.option("--json-output", "json_output", is_flag=True, help="Emit machine-readable JSON.")
def status(json_output: bool) -> None:
    """Show queue counts without starting a worker."""
    configure_logging()

    async def _read() -> dict[str, int]:
        await create_pool()
        try:
            return await get_survey_job_counts()
        finally:
            await close_pool()

    counts = asyncio.run(_read())
    payload = {"rcm_version": RCM_VERSION, "jobs": counts}
    if json_output:
        click.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return
    click.echo(f"RCM expected version: {RCM_VERSION}")
    for state, count in counts.items():
        click.echo(f"{state}: {count}")


__all__ = ["survey_group"]
