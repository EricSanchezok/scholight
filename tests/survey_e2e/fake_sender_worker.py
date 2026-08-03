"""Run the real Survey worker with a hermetic completion-email boundary."""

from __future__ import annotations

import asyncio

from scholight.config import validate_survey_worker_settings
from scholight.db.client import close_pool, create_pool
from scholight.logging import configure_logging
from scholight.survey.email_notifications import SurveyEmail
from scholight.survey.worker import serve_survey_worker


class _FakeEmailSender:
    async def send(self, *, to_address: str, message: SurveyEmail) -> None:
        if not to_address.endswith("@example.com"):
            raise AssertionError("E2E notification used an unexpected recipient domain")
        if not message.subject or not message.html_body or not message.text_body:
            raise AssertionError("E2E notification was incomplete")


async def _run() -> None:
    validate_survey_worker_settings()
    await create_pool()
    try:
        await serve_survey_worker(email_sender=_FakeEmailSender())
    finally:
        await close_pool()


if __name__ == "__main__":
    configure_logging()
    asyncio.run(_run())
