"""Academic Compass logging — structlog + stdlib integration."""

from compass.logging.config import configure_logging
from compass.logging.middleware import RequestContextMiddleware, TimingMiddleware

__all__ = ["RequestContextMiddleware", "TimingMiddleware", "configure_logging"]
