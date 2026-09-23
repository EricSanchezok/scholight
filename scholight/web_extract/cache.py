"""Small process-local content and cursor cache for immutable extraction pages."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from scholight.web_extract.retained_size import INDEX_BYTES, retained_size


@dataclass(frozen=True, slots=True)
class PageSlice:
    content: str
    next_cursor: str | None
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class _Entry:
    actor_key: str
    url: str
    content: str
    metadata: dict[str, object]
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _SizedEntry:
    value: _Entry
    size: int


class ExtractResultCache:
    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_bytes: int,
        max_entries: int = 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._signing_key = secrets.token_bytes(32)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._entries: OrderedDict[str, _SizedEntry] = OrderedDict()
        self._bytes = 0

    def _token(self) -> str:
        return secrets.token_urlsafe(24)

    def _new_cursor(self, entry_id: str, offset: int) -> str:
        payload = f"{entry_id}.{offset}"
        signature = hmac.digest(self._signing_key, payload.encode("ascii"), hashlib.sha256)
        return f"{payload}.{base64.urlsafe_b64encode(signature).decode('ascii').rstrip('=')}"

    def _position(self, cursor: str) -> tuple[str, int] | None:
        if len(cursor) > 128 or not cursor.isascii():
            return None
        parts = cursor.split(".")
        if len(parts) != 3:
            return None
        entry_id, offset, _signature = parts
        if len(entry_id) != 32 or not offset.isdecimal() or len(offset) > 20:
            return None
        position = int(offset)
        if not hmac.compare_digest(cursor, self._new_cursor(entry_id, position)):
            return None
        return entry_id, position

    def _drop(self, entry_id: str) -> None:
        entry = self._entries.pop(entry_id, None)
        if entry is not None:
            self._bytes -= entry.size

    def prune(self) -> None:
        now = self._clock()
        for entry_id, entry in list(self._entries.items()):
            if entry.value.expires_at <= now:
                self._drop(entry_id)
        while self._entries and (
            self._bytes > self._max_bytes or len(self._entries) > self._max_entries
        ):
            self._drop(next(iter(self._entries)))

    def put_private(
        self,
        *,
        actor_key: str,
        url: str,
        content: str,
        metadata: dict[str, object] | None = None,
    ) -> str:
        self.prune()
        entry_id = self._token()
        entry = _Entry(
            actor_key=actor_key,
            url=url,
            content=content,
            metadata=deepcopy(metadata or {}),
            expires_at=self._clock() + self._ttl,
        )
        size = retained_size((entry_id, entry)) + INDEX_BYTES
        # Oversized results must not evict otherwise usable snapshots.
        if size <= self._max_bytes:
            self._entries[entry_id] = _SizedEntry(value=entry, size=size)
            self._bytes += size
            self.prune()
        return self._new_cursor(entry_id, 0)

    def read(self, cursor: str, *, actor_key: str, max_chars: int) -> PageSlice | None:
        self.prune()
        position = self._position(cursor)
        if position is None or max_chars <= 0:
            return None
        entry_id, offset = position
        stored = self._entries.get(entry_id)
        if stored is None or stored.value.actor_key != actor_key:
            return None
        entry = stored.value
        if offset > len(entry.content):
            return None
        self._entries.move_to_end(entry_id)
        content = entry.content[offset : offset + max_chars]
        next_offset = offset + len(content)
        next_cursor = (
            self._new_cursor(entry_id, next_offset) if next_offset < len(entry.content) else None
        )
        return PageSlice(
            content=content, next_cursor=next_cursor, metadata=deepcopy(entry.metadata)
        )


__all__ = ["ExtractResultCache", "PageSlice"]
