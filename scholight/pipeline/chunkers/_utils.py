"""Shared utilities for chunker modules — sentence-level force-split."""

from __future__ import annotations

TARGET_CHARS = 2500  # global ideal chunk size


def _force_split_text(text: str, target: int = TARGET_CHARS) -> list[str]:
    """Split a very long monolithic paragraph at sentence boundaries."""
    result: list[str] = []
    while len(text) > target * 2:
        split_at = target
        for sep in (". ", "? ", "! ", ".\n", "?\n", "!\n"):
            pos = text.rfind(sep, target // 2, target * 3 // 2)
            if pos > 0:
                split_at = pos + 1
                break
        result.append(text[:split_at])
        text = text[split_at:].strip()
    if text:
        result.append(text)
    return result
