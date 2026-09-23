from __future__ import annotations

import pytest
from aiohttp import web

from scholight.api import extract_execution
from scholight.api.tests.test_extract_execution import _actor, _document
from scholight.config import settings
from scholight.models.web_extract import ExtractRequest
from scholight.web_extract.tests.test_fetcher import _server


@pytest.mark.asyncio
async def test_internal_client_is_reused_only_within_its_lifespan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transports: list[object] = []
    request_ids: list[str | None] = []

    async def handler(request: web.Request) -> web.Response:
        transports.append(request.transport)
        request_ids.append(request.headers.get("X-Scholight-Request-Id"))
        return web.json_response(_document().model_dump(mode="json"))

    async with _server(handler) as base:
        monkeypatch.setattr(settings, "extract_service_url", base)
        async with extract_execution.extract_client_lifespan():
            client = extract_execution._internal_client
            assert client is not None
            for request_id in ("first", "second"):
                await extract_execution.execute_public_extract(
                    ExtractRequest.model_validate({"url": "https://example.com"}),
                    extract_execution.ExtractInvocation(_actor(), request_id, "rest"),
                )
        assert client.is_closed
        assert extract_execution._internal_client is None
    assert transports[0] is transports[1]
    assert request_ids == ["first", "second"]
