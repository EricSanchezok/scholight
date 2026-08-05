"""Scholight logging — structlog + stdlib integration."""

from __future__ import annotations

from typing import Any

from scholight.logging.config import configure_logging


def __getattr__(name: str) -> Any:
    """Load HTTP middleware only in API processes that request it."""
    if name in {"RequestContextMiddleware", "TimingMiddleware"}:
        from scholight.logging.middleware import RequestContextMiddleware, TimingMiddleware

        return {
            "RequestContextMiddleware": RequestContextMiddleware,
            "TimingMiddleware": TimingMiddleware,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["RequestContextMiddleware", "TimingMiddleware", "configure_logging"]
