from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def test_expired_cursor_is_not_readable() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    cache = ExtractResultCache(ttl_seconds=60, max_bytes=1024, clock=lambda: now)
    cursor = cache.put_private(actor_key="actor-a", url="https://example.com", content="abcdef")
    cache._clock = lambda: now + timedelta(seconds=61)

    assert cache.read(cursor, actor_key="actor-a", max_chars=3) is None
