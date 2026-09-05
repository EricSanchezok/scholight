"""Bounded, idempotent control plane for one-shot Survey compute tasks."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

import structlog
from botocore.exceptions import BotoCoreError, ClientError

from scholight.config import settings
from scholight.db.queries_survey import SurveyStateError, settle_survey_execution
from scholight.db.queries_survey_attempts import (
    SurveyComputeAttempt,
    count_started_compute_attempts,
    get_compute_attempt_by_task_arn,
    list_active_compute_attempts,
    list_due_compute_attempts,
    mark_compute_attempt_launched,
    mark_compute_attempt_launching,
    record_compute_attempt_stopped,
    record_compute_attempt_succeeded,
    record_compute_launch_failure,
    reserve_next_compute_attempt,
)
from scholight.db.queries_survey_cleanup import (
    claim_artifact_cleanup,
    recover_expired_artifact_cleanups,
)
from scholight.db.queries_survey_notifications import (
    claim_email_notification,
    recover_expired_email_notifications,
)
from scholight.survey.cleanup_worker import process_artifact_cleanup
from scholight.survey.contracts import SurveyLeaseLostError
from scholight.survey.notification_worker import SurveyEmailSender, process_email_notification

logger = structlog.get_logger(__name__)

_MAX_COMPUTE_ATTEMPTS = 3
_MAINTENANCE_BATCH = 2


class ECSClient(Protocol):
    def run_task(self, **kwargs: object) -> dict[str, Any]: ...

    def describe_tasks(self, **kwargs: object) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class SurveyControlConfig:
    cluster_arn: str
    draft_task_definition_arn: str
    full_task_definition_arn: str
    full_high_memory_task_definition_arn: str
    subnet_ids: tuple[str, ...]
    security_group_id: str
    draft_global_concurrency: int = 8
    full_global_concurrency: int = 2
    full_per_user_concurrency: int = 1

    def __post_init__(self) -> None:
        values = (
            self.cluster_arn,
            self.draft_task_definition_arn,
            self.full_task_definition_arn,
            self.full_high_memory_task_definition_arn,
            self.security_group_id,
        )
        if not all(value.strip() for value in values) or not self.subnet_ids:
            raise ValueError("Survey control ECS configuration is incomplete")


class SurveyControl:
    """Run one short reconciliation/dispatch/outbox cycle."""

    def __init__(
        self,
        *,
        config: SurveyControlConfig,
        ecs_client: ECSClient,
        email_sender: SurveyEmailSender | None = None,
    ) -> None:
        self._config = config
        self._ecs = ecs_client
        self._email_sender = email_sender

    @staticmethod
    def _command(attempt: SurveyComputeAttempt) -> tuple[str, list[str]]:
        if attempt.work_kind == "draft":
            if attempt.draft_id is None:
                raise ValueError("Draft attempt has no draft id")
            return (
                "survey-draft",
                [
                    "scholight",
                    "survey",
                    "run-draft",
                    "--draft-id",
                    str(attempt.draft_id),
                    "--attempt-id",
                    str(attempt.id),
                ],
            )
        if attempt.job_id is None:
            raise ValueError("Full attempt has no job id")
        return (
            "survey",
            [
                "scholight",
                "survey",
                "run-job",
                "--job-id",
                str(attempt.job_id),
                "--attempt-id",
                str(attempt.id),
            ],
        )

    async def launch_attempt(self, attempt: SurveyComputeAttempt) -> bool:
        """Launch once with the durable attempt UUID as the ECS idempotency token."""
        launching = await mark_compute_attempt_launching(attempt_id=attempt.id)
        if launching is None:
            return False
        container_name, command = self._command(launching)
        try:
            response = await asyncio.to_thread(
                self._ecs.run_task,
                cluster=self._config.cluster_arn,
                taskDefinition=launching.task_definition_arn,
                launchType="FARGATE",
                platformVersion="LATEST",
                count=1,
                clientToken=str(launching.id),
                startedBy=str(launching.id),
                enableECSManagedTags=True,
                propagateTags="TASK_DEFINITION",
                tags=[{"key": "survey-attempt", "value": str(launching.id)}],
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": list(self._config.subnet_ids),
                        "securityGroups": [self._config.security_group_id],
                        "assignPublicIp": "DISABLED",
                    }
                },
                overrides={"containerOverrides": [{"name": container_name, "command": command}]},
            )
            tasks = response.get("tasks", [])
            failures = response.get("failures", [])
            task_arn = tasks[0].get("taskArn") if tasks else None
            if not isinstance(task_arn, str) or not task_arn:
                await record_compute_launch_failure(
                    attempt_id=launching.id,
                    reason=_launch_failure_reason(failures),
                )
                return False
            await mark_compute_attempt_launched(attempt_id=launching.id, task_arn=task_arn)
            logger.info(
                "survey_compute_task_launched",
                attempt_id=str(launching.id),
                work_kind=launching.work_kind,
                resource_profile=launching.resource_profile,
            )
            return True
        except (BotoCoreError, ClientError, TimeoutError) as exc:
            await record_compute_launch_failure(
                attempt_id=launching.id,
                reason=_aws_error_class(exc),
            )
            logger.warning(
                "survey_compute_task_launch_failed",
                attempt_id=str(launching.id),
                error_type=type(exc).__name__,
            )
            return False

    async def handle_stopped_event(self, event: dict[str, Any]) -> bool:
        """Apply an at-least-once ECS STOPPED event to a tracked task."""
        detail = event.get("detail")
        if not isinstance(detail, dict) or detail.get("lastStatus") != "STOPPED":
            return False
        task_arn = detail.get("taskArn")
        if not isinstance(task_arn, str):
            return False
        attempt = await get_compute_attempt_by_task_arn(task_arn)
        if attempt is None:
            return False
        event_version = _event_version(detail)
        container = _compute_container(detail, work_kind=attempt.work_kind)
        exit_code = _optional_int(container.get("exitCode"))
        stop_code = _optional_str(detail.get("stopCode"))
        stopped_reason = _optional_str(detail.get("stoppedReason"))
        container_reason = _optional_str(container.get("reason"))
        if exit_code == 0:
            await record_compute_attempt_succeeded(
                attempt_id=attempt.id,
                event_version=event_version,
                exit_code=0,
            )
            return True

        failure_class, normalized_reason = _classify_stop(
            exit_code=exit_code,
            stop_code=stop_code,
            stopped_reason=stopped_reason,
            container_reason=container_reason,
        )
        compute_starts = await count_started_compute_attempts(attempt_id=attempt.id)
        retryable = compute_starts < _MAX_COMPUTE_ATTEMPTS
        if failure_class == "oom" and attempt.resource_profile == "full-high-memory":
            retryable = False
        if not retryable and attempt.work_kind == "full" and attempt.job_id is not None:
            try:
                await settle_survey_execution(
                    job_id=attempt.job_id,
                    worker_id=attempt.id,
                    outcome="failed",
                    error_code=(
                        "survey_compute_oom" if failure_class == "oom" else "survey_compute_failed"
                    ),
                    error_message="Survey generation could not be completed.",
                    chargeable=False,
                )
            except (SurveyLeaseLostError, SurveyStateError):
                logger.info(
                    "survey_compute_terminal_settlement_already_owned",
                    attempt_id=str(attempt.id),
                )
        await record_compute_attempt_stopped(
            attempt_id=attempt.id,
            event_version=event_version,
            exit_code=exit_code,
            stop_code=stop_code,
            stopped_reason=stopped_reason,
            failure_class=failure_class,
            failure_details={"container_reason": normalized_reason},
            retryable=retryable,
        )
        return True

    async def reconcile(self) -> int:
        """Recover STOPPED tasks even when their EventBridge event was missed."""
        attempts = await list_active_compute_attempts(limit=32)
        by_arn = {
            attempt.ecs_task_arn: attempt
            for attempt in attempts
            if attempt.ecs_task_arn is not None
        }
        if not by_arn:
            return 0
        response = await asyncio.to_thread(
            self._ecs.describe_tasks,
            cluster=self._config.cluster_arn,
            tasks=list(by_arn),
        )
        handled = 0
        for task in response.get("tasks", []):
            if task.get("lastStatus") != "STOPPED":
                continue
            if await self.handle_stopped_event({"detail": task}):
                handled += 1
        return handled

    async def dispatch(self) -> int:
        """Launch due reservations first, then reserve fair capacity from PostgreSQL."""
        launched = 0
        for attempt in await list_due_compute_attempts(limit=16):
            launched += int(await self.launch_attempt(attempt))
        launched += await self._reserve_and_launch("draft", self._config.draft_global_concurrency)
        launched += await self._reserve_and_launch("full", self._config.full_global_concurrency)
        return launched

    async def _reserve_and_launch(self, work_kind: str, limit: int) -> int:
        launched = 0
        for _ in range(limit):
            if work_kind == "draft":
                attempt = await reserve_next_compute_attempt(
                    work_kind="draft",
                    task_definition_arn=self._config.draft_task_definition_arn,
                    global_concurrency=self._config.draft_global_concurrency,
                    per_user_concurrency=self._config.draft_global_concurrency,
                    max_compute_attempts=_MAX_COMPUTE_ATTEMPTS,
                )
            else:
                attempt = await reserve_next_compute_attempt(
                    work_kind="full",
                    task_definition_arn=self._config.full_task_definition_arn,
                    high_memory_task_definition_arn=(
                        self._config.full_high_memory_task_definition_arn
                    ),
                    global_concurrency=self._config.full_global_concurrency,
                    per_user_concurrency=self._config.full_per_user_concurrency,
                    max_compute_attempts=_MAX_COMPUTE_ATTEMPTS,
                )
            if attempt is None:
                break
            launched += int(await self.launch_attempt(attempt))
        return launched

    async def process_maintenance(self) -> tuple[int, int]:
        """Drain bounded notification and cleanup outbox batches."""
        await recover_expired_artifact_cleanups()
        await recover_expired_email_notifications()
        cleanup_tasks: list[asyncio.Task[None]] = []
        email_tasks: list[asyncio.Task[None]] = []
        for _ in range(_MAINTENANCE_BATCH):
            worker_id = uuid4()
            cleanup = await claim_artifact_cleanup(
                worker_id=worker_id,
                lease_seconds=settings.survey_lease_seconds,
            )
            if cleanup is None:
                break
            cleanup_tasks.append(
                asyncio.create_task(process_artifact_cleanup(cleanup, worker_id=worker_id))
            )
        if self._email_sender is not None:
            for _ in range(_MAINTENANCE_BATCH):
                worker_id = uuid4()
                notification = await claim_email_notification(
                    worker_id=worker_id,
                    lease_seconds=settings.survey_lease_seconds,
                )
                if notification is None:
                    break
                email_tasks.append(
                    asyncio.create_task(
                        process_email_notification(
                            notification,
                            worker_id=worker_id,
                            sender=self._email_sender,
                        )
                    )
                )
        await asyncio.gather(*cleanup_tasks, *email_tasks)
        return len(cleanup_tasks), len(email_tasks)

    async def run_cycle(self, event: dict[str, Any]) -> dict[str, int]:
        event_stops = int(await self.handle_stopped_event(event))
        reconciled = await self.reconcile()
        launched = await self.dispatch()
        cleanups, notifications = await self.process_maintenance()
        return {
            "event_stops": event_stops,
            "reconciled": reconciled,
            "launched": launched,
            "cleanups": cleanups,
            "notifications": notifications,
        }


def _launch_failure_reason(failures: object) -> str:
    if not isinstance(failures, list) or not failures:
        return "ecs_no_task"
    first = failures[0]
    if not isinstance(first, dict):
        return "ecs_launch_failure"
    reason = str(first.get("reason", "")).upper()
    for marker in ("RESOURCE", "ATTRIBUTE", "AGENT", "PLATFORM", "ACCESSDENIED"):
        if marker in reason:
            return f"ecs_{marker.lower()}"
    return "ecs_launch_failure"


def _aws_error_class(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        code = str(exc.response.get("Error", {}).get("Code", "")).lower()
        allowed = {
            "accessdeniedexception": "ecs_access_denied",
            "clientexception": "ecs_client_error",
            "clusterNotfoundexception": "ecs_cluster_missing",
            "serverexception": "ecs_server_error",
            "throttlingexception": "ecs_throttled",
        }
        return allowed.get(code, "ecs_client_error")
    return "ecs_transport_error"


def _event_version(detail: dict[str, Any]) -> int:
    value = detail.get("version", 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _compute_container(detail: dict[str, Any], *, work_kind: str) -> dict[str, Any]:
    expected = "survey-draft" if work_kind == "draft" else "survey"
    containers = detail.get("containers", [])
    if not isinstance(containers, list):
        return {}
    for container in containers:
        if isinstance(container, dict) and container.get("name") == expected:
            return container
    return next((item for item in containers if isinstance(item, dict)), {})


def _classify_stop(
    *,
    exit_code: int | None,
    stop_code: str | None,
    stopped_reason: str | None,
    container_reason: str | None,
) -> tuple[str, str]:
    combined = " ".join(
        value.lower()
        for value in (stop_code, stopped_reason, container_reason)
        if value is not None
    )
    if exit_code == 137 or "outofmemory" in combined or "out of memory" in combined:
        return "oom", "OutOfMemoryError"
    if "cannotpullcontainer" in combined or "resourceinitialization" in combined:
        return "ecs_start", "TaskStartFailure"
    if "spotinterruption" in combined or "terminationnotice" in combined:
        return "ecs_interruption", "TaskInterrupted"
    return "process_exit", "NonZeroExit"


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: object) -> int | None:
    try:
        return int(str(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


__all__ = ["ECSClient", "SurveyControl", "SurveyControlConfig"]
