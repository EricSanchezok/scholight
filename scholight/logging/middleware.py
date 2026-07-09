"""FastAPI middleware for request-scoped logging context.

Two middlewares:
  - RequestContextMiddleware: binds request_id, method, path, peer
    to structlog contextvars so every log entry during the request
    automatically carries these fields.
  - TimingMiddleware: logs request latency; warns on slow requests.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp
from structlog import get_logger
from structlog.contextvars import bind_contextvars, clear_contextvars

logger = get_logger(__name__)


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Bind a unique request_id + HTTP metadata to every log entry.

    Add to FastAPI/Starlette app:
        app.add_middleware(RequestContextMiddleware)
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        clear_contextvars()
        bind_contextvars(
            request_id=str(uuid.uuid4()),
            method=request.method,
            path=request.url.path,
            peer=request.client.host if request.client else "unknown",
        )
        return await call_next(request)


class TimingMiddleware(BaseHTTPMiddleware):
    """Log request latency; emit WARNING when duration exceeds threshold.

    Args:
        slow_threshold_ms: WARNING logged if elapsed >= this value.
                           Default 5000 (5 seconds).
    """

    def __init__(self, app: ASGIApp, *, slow_threshold_ms: float = 5000.0) -> None:
        super().__init__(app)
        self._slow_threshold_ms = slow_threshold_ms

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        elapsed_ms = (time.monotonic() - start) * 1000

        if elapsed_ms >= self._slow_threshold_ms:
            logger.warning("request_slow", duration_ms=round(elapsed_ms, 1))
        else:
            logger.debug("request_complete", duration_ms=round(elapsed_ms, 1))
        return response
