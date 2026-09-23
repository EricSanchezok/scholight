from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scholight.models.web_extract import ExtractResponseFormat, RenderMode
from scholight.web_extract.contracts import InternalExtractResponse
from scholight.web_extract.engine import ExtractInput
from scholight.web_extract.service import _response_from_document, _SharedCache
from scholight.web_extract.tests.test_service import _Engine

pytestmark = pytest.mark.asyncio


async def _document() -> InternalExtractResponse:

    return _response_from_document(
        await _Engine().extract(
            ExtractInput(
                url="https://example.com",
                render=RenderMode.NEVER,
                output=ExtractResponseFormat.MAIN_MARKDOWN,
            )
        )
    )


async def test_shared_cache_counts_metadata_and_wide_strings() -> None:
    response = (await _document()).model_copy(update={"title": "a" * 2000 + "😀"})
    cache = _SharedCache(ttl_seconds=600, max_bytes=4096)
    cache.put("key", response)
    assert cache.get("key") is None


async def test_shared_cache_entry_limit_is_bounded() -> None:
    cache = _SharedCache(ttl_seconds=600, max_bytes=100_000, max_entries=2)
    for key in ["first", "second", "third"]:
        cache.put(key, await _document())
    assert cache.get("first") is None
    assert len(cache._entries) == 2


async def test_shared_cache_sweeps_expiry_without_new_requests() -> None:
    now = datetime.now(UTC)
    cache = _SharedCache(ttl_seconds=600, max_bytes=100_000, clock=lambda: now)
    cache.put("key", await _document())
    now += timedelta(seconds=601)
    cache.prune()
    assert cache._bytes == 0
