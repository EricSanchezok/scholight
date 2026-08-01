"""Small, stable Survey request and worker contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from scholight.db.client import DBError

INITIAL_REQUEST_MAX_BYTES = 64 * 1024
REVISION_MESSAGE_MAX_BYTES = 64 * 1024
MANUAL_DRAFT_MAX_BYTES = 1024 * 1024
DRAFT_CONTEXT_MAX_BYTES = 8 * 1024 * 1024
DRAFT_OUTPUT_MAX_BYTES = 2 * 1024 * 1024
STDERR_TAIL_MAX_BYTES = 64 * 1024

HeartbeatState = Literal["owned", "cancel_requested", "lost", "transient_error"]


class SurveyConflictError(DBError):
    """A stable, user-actionable Survey state conflict."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SurveyLeaseLostError(DBError):
    """A worker no longer owns the task it was processing."""


def utf8_size(value: str) -> int:
    return len(value.encode("utf-8"))


def canonical_request_hash(*, operation: str, payload: dict[str, object]) -> str:
    body = json.dumps(
        {"operation": operation, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


__all__ = [
    "DRAFT_CONTEXT_MAX_BYTES",
    "DRAFT_OUTPUT_MAX_BYTES",
    "HeartbeatState",
    "INITIAL_REQUEST_MAX_BYTES",
    "MANUAL_DRAFT_MAX_BYTES",
    "REVISION_MESSAGE_MAX_BYTES",
    "STDERR_TAIL_MAX_BYTES",
    "SurveyConflictError",
    "SurveyLeaseLostError",
    "canonical_request_hash",
    "utf8_size",
]
