"""Scholight Survey worker operations."""

from __future__ import annotations

import asyncio
import json
import os

# Only a fixed, image-owned executable is invoked below.
import subprocess  # nosec B404
from datetime import UTC, datetime
from pathlib import Path

import click

from scholight.config import (
    settings,
    validate_survey_draft_worker_settings,
    validate_survey_worker_settings,
)
from scholight.db.client import close_pool, create_pool, get_pool
from scholight.db.migration_policy import migration_checksum
from scholight.db.queries_survey import get_survey_job_counts
from scholight.db.queries_survey_cleanup import get_artifact_cleanup_status
from scholight.logging import configure_logging
from scholight.survey.worker import RCM_VERSION, serve_survey_worker


def _installed_rcm_version() -> str:
    # The command and arguments are constants installed in the reviewed image.
    completed = subprocess.run(  # nosec B603
        ["/usr/local/bin/accelerate", "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    output = (completed.stdout or completed.stderr).strip()
    version = output.rsplit(maxsplit=1)[-1].removeprefix("v") if output else ""
    if version != RCM_VERSION:
        raise RuntimeError("Installed RCM version does not match the reviewed release")
    return version


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

    async def _read() -> dict[str, object]:
        await create_pool()
        try:
            jobs = await get_survey_job_counts()
            cleanup = await get_artifact_cleanup_status()
            oldest_age = (
                max(0, int((datetime.now(UTC) - cleanup.oldest_waiting_at).total_seconds()))
                if cleanup.oldest_waiting_at is not None
                else None
            )
            return {
                "rcm_version": RCM_VERSION,
                "jobs": jobs,
                "cleanup": {
                    "pending": cleanup.pending,
                    "running": cleanup.running,
                    "retry": cleanup.retry,
                    "succeeded": cleanup.succeeded,
                    "dead": cleanup.dead,
                    "oldest_waiting_seconds": oldest_age,
                },
                "concurrency": {
                    "draft": settings.survey_draft_concurrency,
                    "draft_per_user": settings.survey_draft_per_user_concurrency,
                    "survey": settings.survey_job_concurrency,
                    "survey_per_user": settings.survey_job_per_user_concurrency,
                },
            }
        finally:
            await close_pool()

    payload = asyncio.run(_read())
    if json_output:
        click.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
        return
    click.echo(f"RCM expected version: {RCM_VERSION}")
    jobs = payload["jobs"]
    if not isinstance(jobs, dict):
        raise click.ClickException("Survey status response is invalid")
    for state, count in jobs.items():
        click.echo(f"{state}: {count}")


@survey_group.command("smoke")
@click.option("--json-output", "json_output", is_flag=True, help="Emit machine-readable JSON.")
def smoke(json_output: bool) -> None:
    """Validate migrations, cleanup health, RCM identity, and private artifact access."""
    configure_logging()
    try:
        validate_survey_worker_settings()
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    async def _run() -> dict[str, object]:
        from scholight.survey.artifacts import SurveyArtifactStore

        installed_rcm_version = await asyncio.to_thread(_installed_rcm_version)
        await create_pool()
        try:
            rows = await get_pool().fetch(
                "SELECT version, name, checksum FROM scholight.schema_migrations "
                "WHERE version IN (6,7,8) ORDER BY version"
            )
            migrations_dir = Path(
                os.environ.get(
                    "SCHOLIGHT_MIGRATIONS_DIR",
                    Path(__file__).resolve().parents[2] / "migrations",
                )
            )
            expected = []
            for version, name in (
                (6, "survey_aggregate"),
                (7, "survey_reliability"),
                (8, "survey_cancellation"),
            ):
                path = migrations_dir / f"{version:03d}_{name}.sql"
                expected.append(
                    (version, name, migration_checksum(path.read_text(encoding="utf-8")))
                )
            applied = [
                (int(row["version"]), str(row["name"]), str(row["checksum"])) for row in rows
            ]
            if applied != expected:
                raise RuntimeError("Survey database migrations are incomplete or modified")
            cleanup = await get_artifact_cleanup_status()
            if cleanup.dead:
                raise RuntimeError("Survey artifact cleanup has dead tasks")
            store = SurveyArtifactStore(
                bucket=settings.survey_s3_bucket,
                endpoint_url=settings.survey_s3_endpoint_url,
            )
            await store.verify_access()
            return {
                "ok": True,
                "rcm_version": installed_rcm_version,
                "migrations": [version for version, _name, _checksum in applied],
                "cleanup_dead": cleanup.dead,
                "concurrency": {
                    "draft": settings.survey_draft_concurrency,
                    "draft_per_user": settings.survey_draft_per_user_concurrency,
                    "survey": settings.survey_job_concurrency,
                    "survey_per_user": settings.survey_job_per_user_concurrency,
                },
            }
        finally:
            await close_pool()

    try:
        payload = asyncio.run(_run())
    except Exception as exc:
        raise click.ClickException("Survey production smoke checks did not pass") from exc
    if json_output:
        click.echo(json.dumps(payload, separators=(",", ":"), sort_keys=True))
    else:
        click.echo("Survey production smoke checks passed.")


__all__ = ["survey_group"]
