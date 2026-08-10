"""Survey queue metrics and ECS task scale-in protection."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from typing import Literal

import httpx
import structlog

from scholight.db.queries_survey_capacity import SurveyQueue, get_survey_capacity_snapshot
from scholight.logging.emf import emit_emf

logger = structlog.get_logger(__name__)
_METRIC_INTERVAL_SECONDS = 30.0
_PROTECTION_REFRESH_SECONDS = 300.0
_PROTECTION_RETRY_SECONDS = 30.0
_PROTECTION_EXPIRES_MINUTES = 30

SurveyQueueOperation = Literal["claim", "heartbeat"]


def emit_survey_database_latency(
    *,
    queue: SurveyQueue,
    service: str,
    operation: SurveyQueueOperation,
    started_at: float,
) -> None:
    """Emit anonymous queue database latency for staged-capacity gates."""
    prefix = "SurveyDraft" if queue == "draft" else "SurveyJob"
    suffix = "ClaimLatency" if operation == "claim" else "HeartbeatLatency"
    emit_emf(
        service=service,
        metrics={
            f"{prefix}{suffix}": (
                (time.perf_counter() - started_at) * 1_000,
                "Milliseconds",
            )
        },
    )


class SurveyTaskProtection:
    """Manage ECS task protection and fail closed before claiming new work."""

    def __init__(
        self,
        *,
        service: str,
        agent_uri: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        base_uri = agent_uri if agent_uri is not None else os.getenv("ECS_AGENT_URI")
        self._endpoint = f"{base_uri.rstrip('/')}/task-protection/v1/state" if base_uri else None
        self._service = service
        self._client = client
        self._protected = False
        self._refresh_at = 0.0
        self._retry_at = 0.0

    @property
    def enabled(self) -> bool:
        return self._endpoint is not None

    async def ensure(self) -> bool:
        """Enable or refresh protection; return false when ECS refuses it."""
        if self._endpoint is None:
            return True
        now = time.monotonic()
        if self._protected and now < self._refresh_at:
            return True
        if now < self._retry_at:
            return False
        if await self._put(enabled=True):
            self._protected = True
            self._refresh_at = now + _PROTECTION_REFRESH_SECONDS
            self._retry_at = 0.0
            return True
        self._retry_at = now + _PROTECTION_RETRY_SECONDS
        return False

    async def release(self) -> None:
        """Clear protection once the task has no active Survey work."""
        if self._endpoint is None or not self._protected:
            return
        if await self._put(enabled=False):
            self._protected = False
            self._refresh_at = 0.0

    async def _put(self, *, enabled: bool) -> bool:
        if self._endpoint is None:
            raise RuntimeError("task protection endpoint is not configured")
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=httpx.Timeout(2.0))
        try:
            payload: dict[str, bool | int] = {"ProtectionEnabled": enabled}
            if enabled:
                payload["ExpiresInMinutes"] = _PROTECTION_EXPIRES_MINUTES
            response = await client.put(
                self._endpoint,
                json=payload,
            )
            response.raise_for_status()
        except (httpx.HTTPError, ValueError):
            logger.exception("survey_task_protection_update_failed", enabled=enabled)
            emit_emf(
                service=self._service,
                metrics={"SurveyTaskProtectionFailure": (1, "Count")},
            )
            return False
        finally:
            if owns_client:
                await client.aclose()
        return True


class SurveyCapacityReporter:
    """Emit one aggregate capacity snapshot at most every 30 seconds."""

    def __init__(self, *, queue: SurveyQueue, service: str, per_user_concurrency: int) -> None:
        self._queue = queue
        self._service = service
        self._per_user_concurrency = per_user_concurrency
        self._next_at = 0.0

    async def emit_if_due(self) -> None:
        now = time.monotonic()
        if now < self._next_at:
            return
        self._next_at = now + _METRIC_INTERVAL_SECONDS
        try:
            snapshot = await get_survey_capacity_snapshot(
                queue=self._queue,
                per_user_concurrency=self._per_user_concurrency,
            )
            oldest_age = (
                max(0, int((datetime.now(UTC) - snapshot.oldest_queued_at).total_seconds()))
                if snapshot.oldest_queued_at is not None
                else 0
            )
            prefix = "SurveyDraft" if self._queue == "draft" else "SurveyJob"
            emit_emf(
                service=self._service,
                metrics={
                    f"{prefix}Queued": (snapshot.queued, "Count"),
                    f"{prefix}Running": (snapshot.running, "Count"),
                    f"{prefix}Outstanding": (snapshot.outstanding, "Count"),
                    f"{prefix}OldestQueuedAge": (oldest_age, "Seconds"),
                    f"{prefix}UsersAtConcurrencyLimit": (snapshot.users_at_limit, "Count"),
                },
            )
        except Exception:
            logger.exception("survey_capacity_metric_failed", queue=self._queue)


__all__ = [
    "SurveyCapacityReporter",
    "SurveyTaskProtection",
    "emit_survey_database_latency",
]
