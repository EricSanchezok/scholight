"""Bounded offline reference policies, deliberately absent from runtime imports."""

from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Access:
    key: str
    retained_bytes: int
    cost_ms: float
    time: float


@dataclass(slots=True)
class Entry:
    access: Access
    charge: int
    expires: float
    priority: float = 0


class Policy:
    extra_entry_bytes = 256
    fixed_bytes = 1024

    def __init__(
        self, *, capacity: int = 32 * 1024 * 1024, max_entries: int = 1024, ttl: float = 600
    ) -> None:
        self.capacity, self.max_entries, self.ttl = capacity, max_entries, ttl
        if capacity < self.fixed_bytes:
            raise ValueError("Capacity cannot be smaller than policy fixed overhead")
        self.entries: OrderedDict[str, Entry] = OrderedDict()
        self.charged_bytes = self.fixed_bytes
        self.peak_bytes = self.fixed_bytes
        self.requests = self.hits = 0
        self.saved_ms = self.total_cost_ms = 0.0
        self._last_time = float("-inf")

    def _observe(self, _key: str) -> None:
        pass

    def _drop(self, key: str) -> Entry:
        entry = self.entries.pop(key)
        self.charged_bytes -= entry.charge
        return entry

    def _store(self, entry: Entry) -> None:
        self.entries[entry.access.key] = entry
        self.charged_bytes += entry.charge

    def _hit(self, key: str) -> None:
        self.entries.move_to_end(key)

    def _insert(self, entry: Entry) -> None:
        while self.entries and (
            self.charged_bytes + entry.charge > self.capacity
            or len(self.entries) >= self.max_entries
        ):
            self._drop(next(iter(self.entries)))
        self._store(entry)

    def access(self, access: Access) -> bool:
        if access.time < self._last_time or access.retained_bytes < 0 or access.cost_ms < 0:
            raise ValueError("Trace must be ordered with nonnegative size and cost")
        self._last_time = access.time
        self.requests += 1
        self.total_cost_ms += access.cost_ms
        self._observe(access.key)
        for key, entry in list(self.entries.items()):
            if entry.expires <= access.time:
                self._drop(key)
        if access.key in self.entries:
            self.hits += 1
            self.saved_ms += access.cost_ms
            self._hit(access.key)
            return True
        charge = access.retained_bytes + self.extra_entry_bytes
        if charge + self.fixed_bytes <= self.capacity and self.max_entries > 0:
            self._insert(Entry(access, charge, access.time + self.ttl))
        self.peak_bytes = max(self.peak_bytes, self.charged_bytes)
        return False

    def report(self) -> dict[str, float | int]:
        return {
            "requests": self.requests,
            "hits": self.hits,
            "hit_ratio": self.hits / max(1, self.requests),
            "saved_ms": self.saved_ms,
            "saved_cost_ratio": self.saved_ms / max(1, self.total_cost_ms),
            "peak_charged_bytes": self.peak_bytes,
            "final_entries": len(self.entries),
        }


class LRU(Policy):
    pass


class GreedyDualSize(Policy):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.inflation = 0.0

    def _hit(self, key: str) -> None:
        entry = self.entries[key]
        entry.priority = self.inflation + entry.access.cost_ms / max(1, entry.charge)

    def _insert(self, entry: Entry) -> None:
        while self.entries and (
            self.charged_bytes + entry.charge > self.capacity
            or len(self.entries) >= self.max_entries
        ):
            # Bounded reference scan avoids lazy priority-queue history growth.
            victim = min(self.entries, key=lambda key: self.entries[key].priority)
            self.inflation = self.entries[victim].priority
            self._drop(victim)
        entry.priority = self.inflation + entry.access.cost_ms / max(1, entry.charge)
        self._store(entry)


class FrequencySketch:
    """Four rows of saturating four-bit counts and an aging Bloom doorkeeper."""

    def __init__(self, *, width: int = 2048, reset_after: int = 10_240) -> None:
        self.width = width
        self.rows = bytearray(width * 2)
        self.door = bytearray(width)
        self.reset_after, self.observed = reset_after, 0

    def _positions(self, key: str) -> tuple[list[int], list[int]]:
        digest = hashlib.blake2b(key.encode(), digest_size=16).digest()
        hashes = [int.from_bytes(digest[i : i + 4], "little") for i in range(0, 16, 4)]
        rows = [row * self.width + value % self.width for row, value in enumerate(hashes)]
        door = [value % (len(self.door) * 8) for value in hashes[:2]]
        return rows, door

    def _get(self, position: int) -> int:
        return (self.rows[position // 2] >> (4 * (position % 2))) & 15

    def observe(self, key: str) -> None:
        self.observed += 1
        if self.observed >= self.reset_after:
            self.rows = bytearray((value >> 1) & 0x77 for value in self.rows)
            self.door = bytearray(len(self.door))
            self.observed = 0
        positions, door = self._positions(key)
        if all(self.door[p // 8] & (1 << (p % 8)) for p in door):
            for p in positions:
                if self._get(p) < 15:
                    self.rows[p // 2] += 1 << (4 * (p % 2))
        else:
            for p in door:
                self.door[p // 8] |= 1 << (p % 8)

    def estimate(self, key: str) -> int:
        positions, door = self._positions(key)
        seen = all(self.door[p // 8] & (1 << (p % 8)) for p in door)
        return min(self._get(p) for p in positions) + int(seen)


class WTinyLFU(Policy):
    # Byte-weighted W-TinyLFU: 1% LRU window, 80% protected main segment.
    # Admission compares aggregate victim frequency for variable-sized objects.
    fixed_bytes = 8192
    extra_entry_bytes = 384

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.window: OrderedDict[str, None] = OrderedDict()
        self.probation: OrderedDict[str, None] = OrderedDict()
        self.protected: OrderedDict[str, None] = OrderedDict()
        self.sketch = FrequencySketch(reset_after=max(100, self.max_entries * 10))
        self.window_limit = max(1, int((self.capacity - self.fixed_bytes) * 0.01))
        self.main_limit = self.capacity - self.fixed_bytes - self.window_limit

    def _observe(self, key: str) -> None:
        self.sketch.observe(key)

    def _size(self, segment: OrderedDict[str, None]) -> int:
        return sum(self.entries[key].charge for key in segment)

    def _drop(self, key: str) -> Entry:
        for segment in (self.window, self.probation, self.protected):
            segment.pop(key, None)
        return super()._drop(key)

    def _hit(self, key: str) -> None:
        if key in self.window:
            self.window.move_to_end(key)
        elif key in self.protected:
            self.protected.move_to_end(key)
        else:
            self.probation.pop(key)
            self.protected[key] = None
            while self.protected and self._size(self.protected) > 0.8 * self.main_limit:
                demoted, _ = self.protected.popitem(last=False)
                self.probation[demoted] = None

    def _admit_main(self, candidate: Entry) -> None:
        if candidate.charge > self.main_limit:
            return
        main_size = self._size(self.probation) + self._size(self.protected)
        victims = []
        freed = 0
        for key in [*self.probation, *self.protected]:
            if (
                main_size - freed + candidate.charge <= self.main_limit
                and len(self.entries) - len(victims) < self.max_entries
            ):
                break
            victims.append(key)
            freed += self.entries[key].charge
        if victims and self.sketch.estimate(candidate.access.key) <= sum(
            self.sketch.estimate(key) for key in victims
        ):
            return
        for key in victims:
            self._drop(key)
        self._store(candidate)
        self.probation[candidate.access.key] = None

    def _insert(self, entry: Entry) -> None:
        self._store(entry)
        self.window[entry.access.key] = None
        while self.window and (
            self._size(self.window) > self.window_limit or len(self.entries) > self.max_entries
        ):
            candidate = self._drop(next(iter(self.window)))
            self._admit_main(candidate)
