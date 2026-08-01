"""Transport-neutral public Web Extract orchestration tests."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from cloud_auth.models.user import UserRecord

from scholight.api.deps import SearchActor
from scholight.api.extract_execution import (
    ExtractInvocation,
    PublicExtractError,
    execute_public_extract,
    reset_extract_result_cache,
)
from scholight.models.web_extract import ExtractRequest
from scholight.web_extract.service import InternalExtractResponse


def _actor() -> SearchActor:
    return SearchActor(
        user=UserRecord(
            id=42,
            email="extract@example.com",
            password_hash="hash",
            status="active",
            email_verified=True,
        ),
        actor_type="access_key",
    )


def _document(*, content: str = "abcdefgh") -> InternalExtractResponse:
    return InternalExtractResponse(
        requested_url="https://example.com/article",
        final_url="https://example.com/article",
        status_code=200,
        title="Example",
        author=None,
        published_at=None,
        content_type="text/html",
        content=content,
        rendered=False,
        extractor="trafilatura",
        warnings=[],
        content_hash="a" * 64,
        fetched_at=datetime(2026, 8, 1, tzinfo=UTC),
    )


@pytest.fixture(autouse=True)
def _reset_cache() -> None:
    reset_extract_result_cache()


@pytest.mark.asyncio
async def test_extract_pages_one_immutable_result_without_refetch() -> None:
    invocation = ExtractInvocation(actor=_actor(), request_id="request-1", transport="rest")
    with patch(
        "scholight.api.extract_execution._request_document",
        AsyncMock(return_value=_document()),
    ) as request_document:
        first = await execute_public_extract(
            ExtractRequest(url="https://example.com/article", max_chars=4), invocation
        )
        second = await execute_public_extract(
            ExtractRequest(cursor=first.next_cursor, max_chars=4), invocation
        )

    assert (first.content, second.content, second.next_cursor) == ("abcd", "efgh", None)
    assert second.title == "Example"
    request_document.assert_awaited_once()


@pytest.mark.asyncio
async def test_extract_cursor_is_bound_to_authenticated_actor() -> None:
    first_actor = _actor()
    second_actor = SearchActor(
        user=first_actor.user.model_copy(update={"id": 99}),
        actor_type="access_key",
    )
    with patch(
        "scholight.api.extract_execution._request_document",
        AsyncMock(return_value=_document()),
    ):
        first = await execute_public_extract(
            ExtractRequest(url="https://example.com/article", max_chars=4),
            ExtractInvocation(actor=first_actor, request_id="request-1", transport="rest"),
        )
        with pytest.raises(PublicExtractError, match="invalid_cursor"):
            await execute_public_extract(
                ExtractRequest(cursor=first.next_cursor, max_chars=4),
                ExtractInvocation(actor=second_actor, request_id="request-2", transport="rest"),
            )


@pytest.mark.asyncio
async def test_extract_requires_authenticated_tool_identity() -> None:
    with pytest.raises(PublicExtractError, match="authentication_required"):
        await execute_public_extract(
            ExtractRequest(url="https://example.com"),
            ExtractInvocation(actor=None, request_id="request-1", transport="mcp"),
        )
