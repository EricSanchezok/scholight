"""Stable Web Extract failures shared by REST, MCP, and the sidecar."""

from __future__ import annotations


class ExtractError(Exception):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        status_code: int,
        retryable: bool,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after = retry_after


__all__ = ["ExtractError"]
