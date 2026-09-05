"""Best-effort Survey control wakeup contracts."""

from __future__ import annotations

import json
from typing import Any

import pytest
from botocore.exceptions import EndpointConnectionError

from scholight.config import settings
from scholight.survey.wakeup import wake_survey_control


class _Lambda:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def invoke(self, **kwargs: object) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self.fail:
            raise EndpointConnectionError(endpoint_url="https://lambda.invalid")
        return {"StatusCode": 202}


@pytest.mark.asyncio
async def test_event_wakeup_is_async_and_contains_no_work_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "survey_dispatch_mode", "event")
    monkeypatch.setattr(settings, "survey_control_function_name", "survey-control")
    client = _Lambda()

    assert await wake_survey_control(reason="draft_submitted", client=client)

    call = client.calls[0]
    assert call["FunctionName"] == "survey-control"
    assert call["InvocationType"] == "Event"
    payload = call["Payload"]
    assert isinstance(payload, bytes)
    assert json.loads(payload) == {
        "source": "scholight.api",
        "reason": "draft_submitted",
    }


@pytest.mark.asyncio
async def test_wakeup_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "survey_dispatch_mode", "event")
    monkeypatch.setattr(settings, "survey_control_function_name", "survey-control")

    assert not await wake_survey_control(
        reason="survey_execution_submitted", client=_Lambda(fail=True)
    )
