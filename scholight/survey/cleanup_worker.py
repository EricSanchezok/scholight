"""Bounded cleanup supervisor for deleted Survey artifact trees."""

from __future__ import annotations

import asyncio
import shutil
import time
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import structlog

from scholight.config import settings
from scholight.db.queries_survey_cleanup import (
    SurveyArtifactCleanup,
    claim_artifact_cleanup,
    complete_artifact_cleanup,
    heartbeat_artifact_cleanup,
    recover_expired_artifact_cleanups,
    retry_artifact_cleanup,
)
from scholight.survey.artifacts import SurveyArtifactStore
from scholight.survey.contracts import SurveyLeaseLostError

logger = structlog.get_logger(__name__)

_CLEANUP_CONCURRENCY = 2
_MAX_ATTEMPTS = 8
# The worker owns this fixed file inside its private, size-bounded tmpfs.
CLEANUP_HEALTH_PATH = Path("/tmp/scholight-survey-cleanup.heartbeat")  # nosec B108


def _touch_health() -> None:
    CLEANUP_HEALTH_PATH.touch(exist_ok=True)


async def _heartbeat(
    cleanup_id: UUID,
    worker_id: UUID,
    stop: asyncio.Event,
    lease_lost: asyncio.Event,
) -> None:
    last_owned = time.monotonic()
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.survey_heartbeat_seconds)
            return
        except TimeoutError:
            try:
                state = await heartbeat_artifact_cleanup(
                    cleanup_id=cleanup_id,
                    worker_id=worker_id,
                    lease_seconds=settings.survey_lease_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "survey_cleanup_heartbeat_failed",
                    cleanup_id=str(cleanup_id),
                    error_type=type(exc).__name__,
                )
                if time.monotonic() - last_owned < settings.survey_lease_seconds:
                    continue
                state = "lost"
            if state == "owned":
                last_owned = time.monotonic()
                continue
            if state == "lost" or time.monotonic() - last_owned >= settings.survey_lease_seconds:
                lease_lost.set()
                return


async def process_artifact_cleanup(
    cleanup: SurveyArtifactCleanup,
    *,
    worker_id: UUID,
) -> None:
    stop = asyncio.Event()
    lease_lost = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(cleanup.id, worker_id, stop, lease_lost))
    try:
        store = SurveyArtifactStore(
            bucket=cleanup.bucket,
            endpoint_url=settings.survey_s3_endpoint_url,
        )
        await store.cleanup_archive(
            user_id=cleanup.user_id,
            job_id=cleanup.source_job_id,
            storage_prefix=cleanup.storage_prefix,
            manifest_key=cleanup.manifest_key,
        )
        if lease_lost.is_set():
            raise SurveyLeaseLostError("Survey artifact cleanup lease was lost")
        await complete_artifact_cleanup(cleanup_id=cleanup.id, worker_id=worker_id)
        scratch = Path(settings.data_root) / "surveys" / str(cleanup.source_job_id)
        try:
            shutil.rmtree(scratch)
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning(
                "survey_deleted_scratch_cleanup_failed",
                cleanup_id=str(cleanup.id),
                error_type=type(exc).__name__,
            )
    except SurveyLeaseLostError:
        logger.info("survey_cleanup_stopped_after_lease_loss", cleanup_id=str(cleanup.id))
    except Exception as exc:
        dead = cleanup.attempts >= _MAX_ATTEMPTS
        delay = timedelta(seconds=min(3600, 30 * (2 ** min(cleanup.attempts, 7))))
        with suppress(SurveyLeaseLostError):
            await retry_artifact_cleanup(
                cleanup_id=cleanup.id,
                worker_id=worker_id,
                delay=delay,
                error_message="Survey artifact cleanup did not complete.",
                dead=dead,
            )
        logger.error(
            "survey_artifact_cleanup_failed",
            cleanup_id=str(cleanup.id),
            dead=dead,
            error_type=type(exc).__name__,
        )
    finally:
        stop.set()
        await asyncio.gather(heartbeat, return_exceptions=True)


async def serve_artifact_cleanup() -> None:
    active: set[asyncio.Task[None]] = set()
    last_recovery = 0.0
    try:
        while True:
            _touch_health()
            active = {task for task in active if not task.done()}
            now = time.monotonic()
            if now - last_recovery >= 30:
                try:
                    await recover_expired_artifact_cleanups()
                except Exception:
                    logger.exception("survey_cleanup_recovery_failed")
                last_recovery = now
            claimed = False
            while len(active) < _CLEANUP_CONCURRENCY:
                worker_id = uuid4()
                try:
                    cleanup = await claim_artifact_cleanup(
                        worker_id=worker_id,
                        lease_seconds=settings.survey_lease_seconds,
                    )
                except Exception:
                    logger.exception("survey_cleanup_claim_cycle_failed")
                    break
                if cleanup is None:
                    break
                active.add(
                    asyncio.create_task(process_artifact_cleanup(cleanup, worker_id=worker_id))
                )
                claimed = True
            if not claimed:
                await asyncio.sleep(1)
    finally:
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


__all__ = ["CLEANUP_HEALTH_PATH", "process_artifact_cleanup", "serve_artifact_cleanup"]
