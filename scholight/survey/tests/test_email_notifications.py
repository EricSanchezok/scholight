"""Survey completion email template and worker behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from scholight.db.queries_survey_notifications import SurveyEmailNotification
from scholight.survey.email_notifications import (
    AliyunSurveyEmailSender,
    SurveyEmailDeliveryError,
    build_survey_email,
)
from scholight.survey.notification_worker import process_email_notification


def _notification(
    *, outcome: str = "succeeded", attempts: int = 1, verified: bool = True
) -> SurveyEmailNotification:
    now = datetime(2026, 8, 3, 6, 30, tzinfo=UTC)
    return SurveyEmailNotification(
        id=uuid4(),
        survey_id=uuid4(),
        user_id=42,
        survey_outcome=outcome,  # type: ignore[arg-type]
        recipient_email="reader@example.com",
        recipient_verified=verified,
        survey_title='<script>alert("x")</script> 思维链压缩',
        started_at=datetime(2026, 8, 3, 4, 30, tzinfo=UTC),
        finished_at=now,
        status="running",
        attempts=attempts,
        lease_owner=uuid4(),
        lease_expires_at=now,
        created_at=now,
    )


def test_success_email_is_escaped_and_links_to_exact_report() -> None:
    notification = _notification()

    message = build_survey_email(notification, public_web_url="https://scholight.example/")

    assert message.subject == "Your Scholight survey is ready"
    assert "&lt;script&gt;" in message.html_body
    assert "<script>" not in message.html_body
    assert f"https://scholight.example/survey/{notification.survey_id}/report" in message.html_body
    assert "Open report" in message.text_body
    assert "You received this email because" in message.text_body


def test_failure_email_links_to_completed_surveys_without_internal_error() -> None:
    message = build_survey_email(
        _notification(outcome="failed"),
        public_web_url="https://scholight.example",
    )

    assert message.subject == "Your Scholight survey could not be completed"
    assert "https://scholight.example/survey?view=completed" in message.html_body
    assert "Review survey" in message.text_body
    assert "traceback" not in message.html_body.lower()


def test_email_rejects_non_http_public_url() -> None:
    with pytest.raises(ValueError, match="absolute HTTP"):
        build_survey_email(_notification(), public_web_url="javascript:alert(1)")


@pytest.mark.asyncio
async def test_directmail_sender_submits_html_and_text_without_sdk_retries() -> None:
    client = AsyncMock()
    sender = AliyunSurveyEmailSender(
        access_key_id="test-key",
        access_key_secret="test-secret",
        account_name="notifications@example.com",
        from_alias="Scholight",
        reply_to_address=False,
        client=client,
    )
    message = build_survey_email(_notification(), public_web_url="https://scholight.example")

    await sender.send(to_address="reader@example.com", message=message)

    call = client.single_send_mail_with_options_async.await_args
    assert call is not None
    request, runtime = call.args
    assert request.to_address == "reader@example.com"
    assert request.html_body == message.html_body
    assert request.text_body == message.text_body
    assert request.click_trace == "0"
    assert runtime.autoretry is False
    assert runtime.connect_timeout == 5_000
    assert runtime.read_timeout == 30_000


@pytest.mark.asyncio
async def test_notification_success_is_completed_once() -> None:
    notification = _notification()
    sender = AsyncMock()
    worker_id = notification.lease_owner
    assert worker_id is not None

    with (
        patch(
            "scholight.survey.notification_worker.complete_email_notification",
            new_callable=AsyncMock,
        ) as complete,
        patch(
            "scholight.survey.notification_worker.retry_email_notification",
            new_callable=AsyncMock,
        ) as retry,
    ):
        await process_email_notification(notification, worker_id=worker_id, sender=sender)

    sender.send.assert_awaited_once()
    complete.assert_awaited_once_with(notification_id=notification.id, worker_id=worker_id)
    retry.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_delivery_failure_is_deferred() -> None:
    notification = _notification(attempts=2)
    sender = AsyncMock()
    sender.send.side_effect = SurveyEmailDeliveryError("provider_throttled", transient=True)
    worker_id = notification.lease_owner
    assert worker_id is not None

    with patch(
        "scholight.survey.notification_worker.retry_email_notification",
        new_callable=AsyncMock,
    ) as retry:
        await process_email_notification(notification, worker_id=worker_id, sender=sender)

    call = retry.await_args
    assert call is not None
    assert call.kwargs["dead"] is False
    assert call.kwargs["error_code"] == "provider_throttled"
    assert call.kwargs["delay"].total_seconds() == 60


@pytest.mark.asyncio
async def test_permanent_or_exhausted_delivery_failure_is_dead() -> None:
    notification = _notification(attempts=8)
    sender = AsyncMock()
    sender.send.side_effect = SurveyEmailDeliveryError("invalid_recipient", transient=False)
    worker_id = notification.lease_owner
    assert worker_id is not None

    with patch(
        "scholight.survey.notification_worker.retry_email_notification",
        new_callable=AsyncMock,
    ) as retry:
        await process_email_notification(notification, worker_id=worker_id, sender=sender)

    call = retry.await_args
    assert call is not None
    assert call.kwargs["dead"] is True


@pytest.mark.asyncio
async def test_unverified_recipient_is_dead_without_contacting_provider() -> None:
    notification = _notification(verified=False)
    sender = AsyncMock()
    worker_id = notification.lease_owner
    assert worker_id is not None

    with patch(
        "scholight.survey.notification_worker.retry_email_notification",
        new_callable=AsyncMock,
    ) as retry:
        await process_email_notification(notification, worker_id=worker_id, sender=sender)

    sender.send.assert_not_awaited()
    call = retry.await_args
    assert call is not None
    assert call.kwargs["dead"] is True
    assert call.kwargs["error_code"] == "recipient_unavailable"
