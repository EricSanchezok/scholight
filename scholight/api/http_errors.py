"""Small constructor for Scholight-owned public HTTP errors."""

from __future__ import annotations

from fastapi import HTTPException


def http_error(
    status_code: int,
    *,
    code: str,
    message: str,
    retryable: bool,
    retry_after: int | None,
) -> HTTPException:
    """Build one explicit public error without inferring business semantics."""
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "retryable": retryable},
        headers=headers,
    )


__all__ = ["http_error"]
