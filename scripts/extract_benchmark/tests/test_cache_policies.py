from __future__ import annotations

import pytest

from scripts.extract_benchmark.cache_policies import LRU, Access, GreedyDualSize, WTinyLFU


@pytest.mark.parametrize("policy", [LRU, WTinyLFU, GreedyDualSize])
def test_replay_caps_retained_bytes_entries_and_does_not_refresh_ttl(policy) -> None:
    cache = policy(capacity=100_000, max_entries=4, ttl=10)
    assert not cache.access(Access("repeat", 1000, 12, 0))
    assert cache.access(Access("repeat", 1000, 12, 9))
    assert not cache.access(Access("repeat", 1000, 12, 11))
    for index in range(100):
        cache.access(Access(str(index), 3000, 10, 12 + index / 100))
        assert cache.charged_bytes <= 100_000 and len(cache.entries) <= 4
    assert not cache.access(Access("oversize", 100_001, 1, 14))
    assert "oversize" not in cache.entries


def test_lru_known_sequence_evicts_the_oldest_access() -> None:
    cache = LRU(capacity=100_000, max_entries=2)
    for key in ["a", "b", "a", "c"]:
        cache.access(Access(key, 1000, 10, 0))
    assert set(cache.entries) == {"a", "c"}


def test_gds_uses_cost_per_retained_byte_with_inflation() -> None:
    cache = GreedyDualSize(capacity=100_000, max_entries=2)
    for key, cost in [("expensive", 100), ("cheap", 1), ("new", 2)]:
        cache.access(Access(key, 1000, cost, 0))
    assert set(cache.entries) == {"expensive", "new"}
    assert cache.inflation > 0


def test_tinylfu_protects_a_hot_entry_during_a_scan() -> None:
    cache = WTinyLFU(capacity=100_000, max_entries=4)
    for _ in range(30):
        cache.access(Access("hot", 1000, 10, 0))
    for index in range(100):
        cache.access(Access(str(index), 1000, 10, 1))
    assert cache.access(Access("hot", 1000, 10, 2))
