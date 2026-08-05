"""Low-cardinality Survey metric classification."""

from __future__ import annotations


def is_provider_throttled(error_code: str | None) -> bool:
    """Classify sanitized provider rate-limit codes without exposing provider data."""
    normalized = (error_code or "").lower()
    return "rate_limit" in normalized or "throttl" in normalized


__all__ = ["is_provider_throttled"]
