"""Small process-local content and cursor cache for immutable extraction pages."""

from __future__ import annotations

import secrets
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


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
class _Cursor:
    entry_id: str
    offset: int


class ExtractResultCache:
    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_bytes: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_bytes = max_bytes
        self._clock = clock or (lambda: datetime.now(UTC))
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._cursors: dict[str, _Cursor] = {}
        self._bytes = 0

    def _token(self) -> str:
        return secrets.token_urlsafe(24)

    def _new_cursor(self, entry_id: str, offset: int) -> str:
        token = self._token()
        self._cursors[token] = _Cursor(entry_id=entry_id, offset=offset)
        return token

    def _drop(self, entry_id: str) -> None:
        entry = self._entries.pop(entry_id, None)
        if entry is not None:
            self._bytes -= len(entry.content.encode("utf-8"))
        stale = [token for token, cursor in self._cursors.items() if cursor.entry_id == entry_id]
        for token in stale:
            self._cursors.pop(token, None)

    def _prune(self) -> None:
        now = self._clock()
        for entry_id, entry in list(self._entries.items()):
            if entry.expires_at <= now:
                self._drop(entry_id)
        while self._bytes > self._max_bytes and self._entries:
            self._drop(next(iter(self._entries)))

    def put_private(
        self,
        *,
        actor_key: str,
        url: str,
        content: str,
        metadata: dict[str, object] | None = None,
    ) -> str:
        self._prune()
        entry_id = self._token()
        entry = _Entry(
            actor_key=actor_key,
            url=url,
            content=content,
            metadata=dict(metadata or {}),
            expires_at=self._clock() + self._ttl,
        )
        self._entries[entry_id] = entry
        self._bytes += len(content.encode("utf-8"))
        self._prune()
        return self._new_cursor(entry_id, 0)

    def read(self, cursor: str, *, actor_key: str, max_chars: int) -> PageSlice | None:
        self._prune()
        position = self._cursors.get(cursor)
        if position is None:
            return None
        entry = self._entries.get(position.entry_id)
        if entry is None or entry.actor_key != actor_key:
            return None
        self._entries.move_to_end(position.entry_id)
        content = entry.content[position.offset : position.offset + max_chars]
        next_offset = position.offset + len(content)
        next_cursor = (
            self._new_cursor(position.entry_id, next_offset)
            if next_offset < len(entry.content)
            else None
        )
        return PageSlice(content=content, next_cursor=next_cursor, metadata=dict(entry.metadata))


__all__ = ["ExtractResultCache", "PageSlice"]
