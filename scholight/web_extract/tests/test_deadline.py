from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

from scholight.api.extract_execution import (
    ExtractInvocation,
    PublicExtractError,
    execute_public_extract,
)
from scholight.api.tests.test_extract_execution import _actor
from scholight.config import settings
from scholight.models.web_extract import ExtractRequest
from scholight.web_extract.service import create_extract_service
from scholight.web_extract.telemetry import current_trace
from scholight.web_extract.tests.test_service import _Engine

pytestmark = pytest.mark.asyncio


async def test_internal_budget_includes_work_and_cancels_the_engine() -> None:
    stopped = asyncio.Event()

    class SlowEngine(_Engine):
        async def extract(self, request):
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()

    app = create_extract_service(engine=SlowEngine(), internal_token="internal-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://extract"
    ) as client:
        response = await client.post(
            "/v1/extract",
            json={"url": "https://example.com"},
            headers={
                "X-Scholight-Internal-Token": "internal-secret",
                "X-Scholight-Budget-Ms": "2050",
            },
        )
    assert response.status_code == 504
    assert stopped.is_set()


async def test_public_deadline_bounds_a_client_that_never_raises_httpx_timeout(monkeypatch) -> None:
    stopped = asyncio.Event()

    async def forever(_request):
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()

    monkeypatch.setattr(settings, "extract_request_timeout_seconds", 0.05)
    with patch("scholight.api.extract_execution._request_document", forever):
        with pytest.raises(PublicExtractError) as error:
            await execute_public_extract(
                ExtractRequest.model_validate({"url": "https://example.com"}),
                ExtractInvocation(actor=_actor(), request_id="deadline-test", transport="rest"),
            )
    assert error.value.code == "extract_timeout"
    assert stopped.is_set()


async def test_internal_completion_is_emitted_once_for_unexpected_failure() -> None:
    class BrokenEngine(_Engine):
        async def extract(self, request):
            raise RuntimeError("untrusted secret")

    app = create_extract_service(engine=BrokenEngine(), internal_token="internal-secret")
    with patch("scholight.web_extract.service.emit_emf") as emit:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://extract",
        ) as client:
            response = await client.post(
                "/v1/extract",
                json={"url": "https://example.com"},
                headers={"X-Scholight-Internal-Token": "internal-secret"},
            )
    assert response.status_code == 503
    completions = [c for c in emit.call_args_list if "RequestCount" in c.kwargs["metrics"]]
    assert len(completions) == 1
    assert "untrusted secret" not in repr(completions)


async def test_cache_hits_report_zero_download_bytes() -> None:
    app = create_extract_service(engine=_Engine(), internal_token="internal-secret")
    with patch("scholight.web_extract.service.emit_emf") as emit:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://extract"
        ) as client:
            for _ in range(2):
                await client.post(
                    "/v1/extract",
                    json={"url": "https://example.com"},
                    headers={"X-Scholight-Internal-Token": "internal-secret"},
                )
    assert emit.call_args.kwargs["metrics"]["DownloadBytes"] == (0, "Bytes")


async def test_completion_logging_failure_preserves_result_and_trace_context() -> None:
    app = create_extract_service(engine=_Engine(), internal_token="internal-secret")
    with patch("scholight.web_extract.telemetry.logger.info", side_effect=OSError("closed pipe")):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://extract"
        ) as client:
            response = await client.post(
                "/v1/extract",
                json={"url": "https://example.com"},
                headers={"X-Scholight-Internal-Token": "internal-secret"},
            )
    assert response.status_code == 200
    assert current_trace.get() is None


async def test_public_disconnect_cancels_internal_work_with_a_controlled_result() -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def forever(_request):
        started.set()
        try:
            await asyncio.sleep(10)
        finally:
            stopped.set()

    with patch("scholight.api.extract_execution._request_document", forever):
        with pytest.raises(PublicExtractError) as error:
            await execute_public_extract(
                ExtractRequest.model_validate({"url": "https://example.com"}),
                ExtractInvocation(
                    actor=_actor(),
                    request_id="disconnect-test",
                    transport="rest",
                    wait_for_disconnect=started.wait,
                ),
            )
    assert error.value.status_code == 499
    assert stopped.is_set()
