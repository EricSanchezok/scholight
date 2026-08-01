"""Public REST Web Extract contract tests."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from pydantic import AnyHttpUrl

from scholight.api.deps import SearchActor, get_extract_actor
from scholight.models.web_extract import ExtractResponse


@pytest.mark.asyncio
async def test_extract_rest_uses_access_key_actor(api_app: FastAPI, active_user: object) -> None:
    actor = SearchActor(user=active_user, actor_type="access_key")  # type: ignore[arg-type]
    api_app.dependency_overrides[get_extract_actor] = lambda: actor
    expected = ExtractResponse(
        requested_url=AnyHttpUrl("https://example.com"),
        final_url=AnyHttpUrl("https://example.com"),
        status_code=200,
        title="Example",
        author=None,
        published_at=None,
        content_type="text/html",
        content="# Example",
        rendered=False,
        extractor="trafilatura",
        warnings=[],
        content_hash="a" * 64,
        fetched_at=datetime(2026, 8, 1, tzinfo=UTC),
        truncated=False,
        next_cursor=None,
    )
    with patch(
        "scholight.api.routes.extract.execute_public_extract",
        AsyncMock(return_value=expected),
    ) as execute:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app), base_url="http://test"
        ) as client:
            response = await client.post("/extract", json={"url": "https://example.com"})

    assert response.status_code == 200
    assert response.json()["content"] == "# Example"
    execute_call = execute.await_args
    assert execute_call is not None
    assert execute_call.args[1].actor is actor


def test_extract_openapi_requires_bearer_access_key(api_app: FastAPI) -> None:
    operation = api_app.openapi()["paths"]["/extract"]["post"]

    assert operation["security"] == [{"BearerAuth": []}]
