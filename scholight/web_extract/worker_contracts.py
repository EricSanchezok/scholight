"""Private file-based IPC; public and internal HTTP JSON contracts stay unchanged."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from scholight.web_extract.contracts import InternalExtractRequest


class FetchMetadata(BaseModel):
    requested_url: str
    final_url: str
    status_code: int
    content_type: str
    charset: str | None


class WorkerJob(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: InternalExtractRequest
    result_path: str
    result_limit: int
    body_path: str = ""
    fetched: FetchMetadata | None = None
    rendered: bool = False


class WorkerFailure(BaseModel):
    code: str
    message: str
    status_code: int
    retryable: bool
