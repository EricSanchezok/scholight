"""Explicit static GET retry classification and a transport without hidden replay."""

from __future__ import annotations

import re
import socket
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import aiohttp

from scholight.web_extract.errors import ExtractError

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class FetchAttemptError(ExtractError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        retryable: bool,
        transient: bool,
        retry_delay: float | None = None,
    ) -> None:
        super().__init__(code=code, message=message, status_code=status_code, retryable=retryable)
        self.transient = transient
        self.retry_delay = retry_delay


def retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    if value is None or len(value) > 128:
        return None
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value):
        return float(value)
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            date = date.replace(tzinfo=UTC)
        return max(0, (date - (now or datetime.now(UTC))).total_seconds())
    except (ValueError, TypeError, OverflowError):
        return None


def network_failure(error: Exception) -> FetchAttemptError:
    transient = isinstance(error, aiohttp.ClientConnectionError)
    if isinstance(error, (aiohttp.ClientSSLError, aiohttp.ClientConnectorCertificateError)):
        transient = False
    if isinstance(error, aiohttp.ClientConnectorDNSError):
        transient = error.os_error.errno == socket.EAI_AGAIN
    if isinstance(error, TimeoutError):
        return FetchAttemptError(
            code="fetch_timeout",
            message="Target did not respond before the timeout.",
            status_code=504,
            retryable=True,
            transient=isinstance(error, aiohttp.ConnectionTimeoutError),
        )
    return FetchAttemptError(
        code="target_unreachable",
        message="Target could not be reached.",
        status_code=502,
        retryable=True,
        transient=transient,
    )


async def single_attempt(
    request: aiohttp.ClientRequest,
    handler: Callable[[aiohttp.ClientRequest], Awaitable[aiohttp.ClientResponse]],
) -> aiohttp.ClientResponse:
    try:
        return await handler(request)
    except (aiohttp.ClientError, TimeoutError) as error:
        # Translate at the public middleware boundary before aiohttp can replay.
        raise network_failure(error) from error
