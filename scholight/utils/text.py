"""Small text helpers shared by ingestion and maintenance tools."""

from __future__ import annotations


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Fit text into a UTF-8 byte limit without splitting a code point."""
    encoded = value.encode()
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode(errors="ignore")
