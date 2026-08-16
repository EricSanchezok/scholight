"""Scholight-branded Survey notification emails and Aliyun DirectMail delivery."""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import UTC
from typing import Any
from urllib.parse import urlsplit

from Tea.exceptions import TeaException

from scholight.db.queries_survey_notifications import SurveyEmailNotification


@dataclass(frozen=True, slots=True)
class SurveyEmail:
    subject: str
    html_body: str
    text_body: str


class SurveyEmailDeliveryError(RuntimeError):
    """A sanitized provider failure with an explicit retry policy."""

    def __init__(self, code: str, *, transient: bool) -> None:
        super().__init__(code)
        self.code = code
        self.transient = transient


def _public_base_url(value: str) -> str:
    base = value.strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Public web URL must be an absolute HTTP(S) URL")
    return base


def _formatted_time(notification: SurveyEmailNotification) -> str:
    return notification.finished_at.astimezone(UTC).strftime("%b %-d, %Y at %-I:%M %p UTC")


def _duration(notification: SurveyEmailNotification) -> str | None:
    if notification.started_at is None:
        return None
    seconds = max(0, round((notification.finished_at - notification.started_at).total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _failure_message(error_code: str | None) -> str:
    if error_code in {
        "survey_report_missing",
        "survey_contract_violation",
        "survey_artifact_contract_invalid",
        "survey_outline_metadata_invalid",
        "survey_section_contract_invalid",
        "survey_reference_contract_invalid",
        "survey_finalization_write_failed",
        "survey_finalization_output_invalid",
    }:
        return (
            "The research materials were saved, but Scholight could not assemble the final "
            "report. The run can be reviewed for recovery."
        )
    if error_code in {
        "survey_model_rate_limited",
        "survey_provider_unavailable",
        "survey_timed_out",
    }:
        return "The survey stopped because a research provider was temporarily unavailable."
    return "The survey ended before a report was created. Open Scholight to review its status."


def build_survey_email(
    notification: SurveyEmailNotification,
    *,
    public_web_url: str,
) -> SurveyEmail:
    """Build a bounded, client-safe HTML email with an equivalent text body."""
    base = _public_base_url(public_web_url)
    title = notification.survey_title.strip()[:160] or "Your research survey"
    safe_title = html.escape(title, quote=True)
    completed_at = _formatted_time(notification)
    duration = _duration(notification)
    if notification.survey_outcome == "succeeded":
        subject = "Your Scholight survey is ready"
        eyebrow = "SURVEY COMPLETE"
        message = "Your research survey is complete and the report is ready to read."
        action = "Open report"
        action_url = f"{base}/survey/{notification.survey_id}/report"
    else:
        subject = "Your Scholight survey could not be completed"
        eyebrow = "SURVEY UPDATE"
        message = _failure_message(notification.survey_error_code)
        action = "Review survey"
        action_url = f"{base}/survey?view=completed"
    safe_url = html.escape(action_url, quote=True)
    duration_row = (
        f'<tr><td style="padding:4px 0;color:#61636e;font-size:13px;">Research time</td>'
        f'<td style="padding:4px 0;text-align:right;color:#2e2f36;font-size:13px;">'
        f"{html.escape(duration)}</td></tr>"
        if duration is not None
        else ""
    )
    html_body = f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width"></head>
<body style="margin:0;padding:0;background:#fbfaf5;color:#0e0f14;">
  <div style="display:none;max-height:0;overflow:hidden;opacity:0;">{html.escape(message)}</div>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
         style="width:100%;background:#fbfaf5;">
    <tr><td align="center" style="padding:40px 16px;">
      <table role="presentation" width="600" cellspacing="0" cellpadding="0" border="0"
             style="width:100%;max-width:600px;background:#fffefc;border:1px solid #dbd9cc;">
        <tr><td style="height:4px;background:#1f45b8;font-size:0;line-height:0;">&nbsp;</td></tr>
        <tr><td style="padding:34px 38px 14px;font-family:Arial,sans-serif;">
          <div style="font-family:Georgia,serif;font-size:24px;font-weight:700;">scholight</div>
        </td></tr>
        <tr><td style="padding:18px 38px 8px;font-family:Arial,sans-serif;color:#1f45b8;
                            font-size:12px;font-weight:700;letter-spacing:.06em;">{eyebrow}</td></tr>
        <tr><td style="padding:0 38px;font-family:Georgia,serif;font-size:28px;font-weight:700;
                            line-height:1.3;color:#0e0f14;">{safe_title}</td></tr>
        <tr><td style="padding:20px 38px 0;font-family:Arial,sans-serif;font-size:15px;
                            line-height:1.65;color:#4f5059;">{html.escape(message)}</td></tr>
        <tr><td style="padding:26px 38px 28px;">
          <a href="{safe_url}" style="display:inline-block;padding:13px 22px;border-radius:6px;
             background:#1f45b8;color:#fffefc;font-family:Arial,sans-serif;font-size:14px;
             font-weight:700;text-decoration:none;">{action}</a>
        </td></tr>
        <tr><td style="padding:0 38px 28px;">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0"
                 style="border-top:1px solid #dbd9cc;padding-top:16px;font-family:Arial,sans-serif;">
            <tr><td style="padding:4px 0;color:#61636e;font-size:13px;">Finished</td>
                <td style="padding:4px 0;text-align:right;color:#2e2f36;font-size:13px;">{completed_at}</td></tr>
            {duration_row}
          </table>
        </td></tr>
        <tr><td style="padding:20px 38px 28px;background:#f4f2ec;font-family:Arial,sans-serif;
                            font-size:12px;line-height:1.6;color:#61636e;">
          You received this email because you requested an update when starting this survey.<br>
          If the button does not work, open: <a href="{safe_url}" style="color:#18389e;">{safe_url}</a>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    metadata = f"Finished: {completed_at}"
    if duration is not None:
        metadata += f"\nResearch time: {duration}"
    text_body = (
        f"scholight\n\n{eyebrow}\n{title}\n\n{message}\n\n{action}: {action_url}\n\n"
        f"{metadata}\n\nYou received this email because you requested an update when "
        "starting this survey."
    )
    return SurveyEmail(subject=subject, html_body=html_body, text_body=text_body)


class AliyunSurveyEmailSender:
    """Send one product notification through Aliyun DirectMail."""

    def __init__(
        self,
        *,
        access_key_id: str,
        access_key_secret: str,
        account_name: str,
        from_alias: str,
        reply_to_address: bool,
        client: Any | None = None,
    ) -> None:
        self._account_name = account_name
        self._from_alias = from_alias
        self._reply_to_address = reply_to_address
        self._client = client or self._build_client(access_key_id, access_key_secret)

    @staticmethod
    def _build_client(access_key_id: str, access_key_secret: str) -> Any:
        from alibabacloud_dm20151123.client import Client as DmClient
        from alibabacloud_tea_openapi import models as open_api_models

        return DmClient(
            open_api_models.Config(
                access_key_id=access_key_id,
                access_key_secret=access_key_secret,
                endpoint="dm.aliyuncs.com",
            )
        )

    async def send(self, *, to_address: str, message: SurveyEmail) -> None:
        from alibabacloud_dm20151123 import models as dm_models
        from alibabacloud_tea_util import models as util_models

        request = dm_models.SingleSendMailRequest(
            account_name=self._account_name,
            from_alias=self._from_alias,
            address_type=1,
            reply_to_address=self._reply_to_address,
            to_address=to_address,
            subject=message.subject,
            html_body=message.html_body,
            text_body=message.text_body,
            click_trace="0",
        )
        runtime = util_models.RuntimeOptions(
            autoretry=False,
            connect_timeout=5_000,
            read_timeout=30_000,
        )
        try:
            await self._client.single_send_mail_with_options_async(request, runtime)
        except Exception as exc:
            if isinstance(exc, TeaException):
                raw_code = str(getattr(exc, "code", "")).lower()
                status_code = int(getattr(exc, "statusCode", 0) or 0)
                transient = (
                    status_code in {408, 429}
                    or status_code >= 500
                    or any(
                        marker in raw_code
                        for marker in ("throttl", "timeout", "serviceunavailable", "internalerror")
                    )
                )
                code = "provider_throttled" if "throttl" in raw_code else "provider_rejected"
                raise SurveyEmailDeliveryError(code, transient=transient) from exc
            raise SurveyEmailDeliveryError("provider_unavailable", transient=True) from exc


__all__ = [
    "AliyunSurveyEmailSender",
    "SurveyEmail",
    "SurveyEmailDeliveryError",
    "build_survey_email",
]
