"""Shared HTTP utilities used across pipeline and search components."""

import httpx


def is_transient(exception: BaseException) -> bool:
    """Return True for transient HTTP errors that should trigger a retry.

    Covers timeouts, network errors, protocol errors, and server-side
    HTTP errors (5xx + 429).  Used by ``Embedder`` to decide which
    failures are retriable.
    """
    if isinstance(exception, httpx.TimeoutException):
        return True
    if isinstance(exception, httpx.NetworkError):
        return True
    if isinstance(exception, httpx.RemoteProtocolError):
        return True
    if isinstance(exception, httpx.HTTPStatusError):
        return exception.response.status_code >= 500 or exception.response.status_code == 429
    return False
