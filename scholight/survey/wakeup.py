"""Best-effort low-latency wakeup for the durable Survey control plane."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

import boto3
import structlog
from botocore.exceptions import BotoCoreError, ClientError

from scholight.config import settings
from scholight.logging.emf import emit_emf

logger = structlog.get_logger(__name__)


class LambdaClient(Protocol):
    def invoke(self, **kwargs: object) -> dict[str, Any]: ...


async def wake_survey_control(*, reason: str, client: LambdaClient | None = None) -> bool:
    """Invoke asynchronously; durable database submission remains authoritative."""
    if settings.survey_dispatch_mode != "event":
        return False
    function_name = settings.survey_control_function_name.strip()
    if not function_name:
        logger.error("survey_control_wakeup_misconfigured")
        _emit_wakeup_metric("misconfigured")
        return False
    lambda_client = client or boto3.client("lambda")
    try:
        response = await asyncio.to_thread(
            lambda_client.invoke,
            FunctionName=function_name,
            InvocationType="Event",
            Payload=json.dumps({"source": "scholight.api", "reason": reason}).encode(),
        )
        accepted = int(response.get("StatusCode", 0)) == 202
    except (BotoCoreError, ClientError, TimeoutError) as exc:
        logger.warning("survey_control_wakeup_failed", error_type=type(exc).__name__)
        _emit_wakeup_metric("failed")
        return False
    _emit_wakeup_metric("accepted" if accepted else "rejected")
    return accepted


def _emit_wakeup_metric(outcome: str) -> None:
    emit_emf(
        service="api",
        outcome=outcome,
        metrics={"SurveyControlWakeupCount": (1, "Count")},
    )


__all__ = ["LambdaClient", "wake_survey_control"]
