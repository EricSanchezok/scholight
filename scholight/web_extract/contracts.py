"""Lightweight wire contracts shared by the API and extract service."""

from __future__ import annotations

from datetime import datetime

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field

from scholight.models.web_extract import ExtractResponseFormat, RenderMode


class InternalExtractRequest(BaseModel):
    """Authenticated request sent from the API to the private extract service."""

    model_config = ConfigDict(extra="forbid")

    url: AnyHttpUrl
    render: RenderMode = RenderMode.AUTO
    output: ExtractResponseFormat = ExtractResponseFormat.MAIN_MARKDOWN
    headers: dict[str, str] = Field(default_factory=dict)
    cookies: dict[str, str] = Field(default_factory=dict)


class InternalExtractResponse(BaseModel):
    """Extract result returned over the private service boundary."""

    model_config = ConfigDict(extra="forbid")

    requested_url: str
    final_url: str
    status_code: int
    title: str | None
    author: str | None
    published_at: str | None
    content_type: str
    content: str
    rendered: bool
    extractor: str
    warnings: list[str]
    content_hash: str
    fetched_at: datetime
    source_bytes: int = Field(ge=0)


__all__ = ["InternalExtractRequest", "InternalExtractResponse"]
