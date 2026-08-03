"""Isolated Survey email notification supervisor."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4

import structlog

from scholight.config import settings
from scholight.db.queries_survey_notifications import (
    SurveyEmailNotification,
    claim_email_notification,
    complete_email_notification,
    get_email_notification_status,
    recover_expired_email_notifications,
    retry_email_notification,
)
from scholight.logging.emf import emit_emf
from scholight.survey.email_notifications import (
    SurveyEmail,
    SurveyEmailDeliveryError,
    build_survey_email,
)

logger = structlog.get_logger(__name__)

_CONCURRENCY = 2
_MAX_ATTEMPTS = 8
_IDLE_SECONDS = 1.0
_RECOVERY_SECONDS = 30.0
_STATUS_METRIC_SECONDS = 60.0


class SurveyEmailSender(Protocol):
    async def send(self, *, to_address: str, message: SurveyEmail) -> None: ...


def _emit_delivery_metric(notification: SurveyEmailNotification, *, outcome: str) -> None:
    latency_ms = max(
        0,
        round((datetime.now(UTC) - notification.created_at).total_seconds() * 1_000),
    )
    emit_emf(
        service="survey-worker",
        outcome=outcome,
        metrics={
            "SurveyEmailNotificationCount": (1, "Count"),
            "SurveyEmailNotificationLatency": (latency_ms, "Milliseconds"),
        },
    )


async def process_email_notification(
    notification: SurveyEmailNotification,
    *,
    worker_id: UUID,
    sender: SurveyEmailSender,
) -> None:
    """Deliver one claimed notification without changing its Survey aggregate."""
    try:
        if not notification.recipient_verified or not notification.recipient_email.strip():
            raise SurveyEmailDeliveryError("recipient_unavailable", transient=False)
        message = build_survey_email(notification, public_web_url=settings.public_web_url)
        await sender.send(to_address=notification.recipient_email, message=message)
        await complete_email_notification(notification_id=notification.id, worker_id=worker_id)
    except SurveyEmailDeliveryError as exc:
        dead = not exc.transient or notification.attempts >= _MAX_ATTEMPTS
        delay = timedelta(seconds=min(3_600, 30 * (2 ** min(max(notification.attempts - 1, 0), 7))))
        await retry_email_notification(
            notification_id=notification.id,
            worker_id=worker_id,
            delay=delay,
            error_code=exc.code,
            dead=dead,
        )
        logger.warning(
            "survey_email_notification_failed",
            notification_id=str(notification.id),
            survey_id=str(notification.survey_id),
            error_code=exc.code,
            attempts=notification.attempts,
            dead=dead,
        )
        _emit_delivery_metric(notification, outcome="dead" if dead else "retry")
        return
    except Exception as exc:
        dead = notification.attempts >= _MAX_ATTEMPTS
        delay = timedelta(seconds=min(3_600, 30 * (2 ** min(max(notification.attempts - 1, 0), 7))))
        await retry_email_notification(
            notification_id=notification.id,
            worker_id=worker_id,
            delay=delay,
            error_code="notification_worker_error",
            dead=dead,
        )
        logger.error(
            "survey_email_notification_failed",
            notification_id=str(notification.id),
            survey_id=str(notification.survey_id),
            error_type=type(exc).__name__,
            attempts=notification.attempts,
            dead=dead,
        )
        _emit_delivery_metric(notification, outcome="dead" if dead else "retry")
        return
    logger.info(
        "survey_email_notification_sent",
        notification_id=str(notification.id),
        survey_id=str(notification.survey_id),
        survey_outcome=notification.survey_outcome,
    )
    _emit_delivery_metric(notification, outcome="sent")


async def serve_email_notifications(sender: SurveyEmailSender) -> None:
    """Supervise bounded notification delivery with lease recovery and queue metrics."""
    active: set[asyncio.Task[None]] = set()
    last_recovery = 0.0
    last_metric = 0.0
    try:
        while True:
            active = {task for task in active if not task.done()}
            now = time.monotonic()
            if now - last_recovery >= _RECOVERY_SECONDS:
                try:
                    await recover_expired_email_notifications()
                except Exception:
                    logger.exception("survey_email_notification_recovery_failed")
                last_recovery = now
            if now - last_metric >= _STATUS_METRIC_SECONDS:
                try:
                    status = await get_email_notification_status()
                    oldest_age = (
                        max(
                            0, round((datetime.now(UTC) - status.oldest_waiting_at).total_seconds())
                        )
                        if status.oldest_waiting_at is not None
                        else 0
                    )
                    emit_emf(
                        service="survey-worker",
                        metrics={
                            "SurveyEmailNotificationPending": (
                                status.pending + status.retry,
                                "Count",
                            ),
                            "SurveyEmailNotificationDead": (status.dead, "Count"),
                            "SurveyEmailNotificationOldestAge": (oldest_age, "Seconds"),
                        },
                    )
                except Exception:
                    logger.exception("survey_email_notification_status_failed")
                last_metric = now
            claimed = False
            while len(active) < _CONCURRENCY:
                worker_id = uuid4()
                try:
                    notification = await claim_email_notification(
                        worker_id=worker_id,
                        lease_seconds=settings.survey_lease_seconds,
                    )
                except Exception:
                    logger.exception("survey_email_notification_claim_cycle_failed")
                    break
                if notification is None:
                    break
                active.add(
                    asyncio.create_task(
                        process_email_notification(
                            notification,
                            worker_id=worker_id,
                            sender=sender,
                        )
                    )
                )
                claimed = True
            if not claimed:
                await asyncio.sleep(_IDLE_SECONDS)
    finally:
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)


__all__ = ["SurveyEmailSender", "process_email_notification", "serve_email_notifications"]
