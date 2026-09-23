from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scholight.web_extract.cache import ExtractResultCache


def test_private_cursor_is_bound_to_actor() -> None:
    cache = ExtractResultCache(ttl_seconds=600, max_bytes=1024)
    cursor = cache.put_private(actor_key="actor-a", url="https://example.com", content="abcdef")

    assert cache.read(cursor, actor_key="actor-b", max_chars=3) is None


def test_private_cursor_pages_stable_content() -> None:
    cache = ExtractResultCache(ttl_seconds=600, max_bytes=1024)
    cursor = cache.put_private(actor_key="actor-a", url="https://example.com", content="abcdef")

    first = cache.read(cursor, actor_key="actor-a", max_chars=3)
    second = cache.read(first.next_cursor or "", actor_key="actor-a", max_chars=3)

    assert (first.content, second.content, second.next_cursor) == ("abc", "def", None)


def test_private_cursor_preserves_document_metadata() -> None:
    cache = ExtractResultCache(ttl_seconds=600, max_bytes=1024)
    cursor = cache.put_private(
        actor_key="actor-a",
        url="https://example.com",
        content="abcdef",
        metadata={"title": "Example"},
    )

    page = cache.read(cursor, actor_key="actor-a", max_chars=3)

    assert page is not None
    assert page.metadata == {"title": "Example"}


def test_expired_cursor_is_not_readable() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    cache = ExtractResultCache(ttl_seconds=60, max_bytes=1024, clock=lambda: now)
    cursor = cache.put_private(actor_key="actor-a", url="https://example.com", content="abcdef")
    cache._clock = lambda: now + timedelta(seconds=61)

    assert cache.read(cursor, actor_key="actor-a", max_chars=3) is None


def test_replaying_a_cursor_does_not_allocate_server_state() -> None:
    cache = ExtractResultCache(ttl_seconds=600, max_bytes=4096)
    cursor = cache.put_private(actor_key="actor-a", url="https://example.com", content="abcdef")
    first = cache.read(cursor, actor_key="actor-a", max_chars=3)
    for _ in range(10_000):
        assert cache.read(cursor, actor_key="actor-a", max_chars=3) == first
    assert len(getattr(cache, "_cursors", {})) == 0


@pytest.mark.parametrize("cursor", ["garbage", "." * 1000, "x.1.bad", "x.-1.bad"])
def test_malformed_signed_cursor_is_rejected(cursor: str) -> None:
    cache = ExtractResultCache(ttl_seconds=600, max_bytes=4096)
    assert cache.read(cursor, actor_key="actor-a", max_chars=3) is None


def test_changing_a_signed_cursor_offset_is_rejected() -> None:
    cache = ExtractResultCache(ttl_seconds=600, max_bytes=4096)
    cursor = cache.put_private(actor_key="actor-a", url="https://example.com", content="abcdef")
    parts = cursor.split(".")
    assert len(parts) == 3
    parts[1] = "1"
    assert cache.read(".".join(parts), actor_key="actor-a", max_chars=3) is None


def test_snapshot_metadata_is_immutable_across_reads() -> None:
    cache = ExtractResultCache(ttl_seconds=600, max_bytes=4096)
    metadata = {"warnings": ["original"]}
    cursor = cache.put_private(
        actor_key="actor-a", url="https://example.com", content="abc", metadata=metadata
    )
    metadata["warnings"].append("changed input")
    page = cache.read(cursor, actor_key="actor-a", max_chars=3)
    assert page is not None
    page.metadata["warnings"].append("changed output")
    assert cache.read(cursor, actor_key="actor-a", max_chars=3).metadata == {
        "warnings": ["original"]
    }


def test_snapshot_counts_metadata_and_wide_unicode_memory() -> None:
    cache = ExtractResultCache(ttl_seconds=600, max_bytes=4096)
    cursor = cache.put_private(
        actor_key="actor-a", url="https://example.com", content="a" * 2000 + "😀"
    )
    assert cache.read(cursor, actor_key="actor-a", max_chars=3) is None


def test_snapshot_entry_limit_evicts_the_least_recently_used() -> None:
    cache = ExtractResultCache(ttl_seconds=600, max_bytes=100_000, max_entries=2)
    cursors = [
        cache.put_private(actor_key="actor-a", url="https://example.com", content=str(i))
        for i in range(3)
    ]
    assert cache.read(cursors[0], actor_key="actor-a", max_chars=3) is None
    assert len(cache._entries) == 2
