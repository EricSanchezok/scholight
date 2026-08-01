"""Public, transport-neutral models for Web Extract."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, field_validator, model_validator

_TRANSPORT_HEADERS = frozenset(
    {"host", "content-length", "transfer-encoding", "connection", "upgrade"}
)


class RenderMode(StrEnum):
    AUTO = "auto"
    NEVER = "never"
    ALWAYS = "always"


class ExtractResponseFormat(StrEnum):
    MAIN_MARKDOWN = "main_markdown"
    FULL_MARKDOWN = "full_markdown"
    TEXT = "text"
    RAW_HTML = "raw_html"


class ExtractRequest(BaseModel):
    """Start an extraction or continue one immutable cached result."""

    model_config = ConfigDict(extra="forbid")

    url: AnyHttpUrl | None = None
    render: RenderMode = RenderMode.AUTO
    output: ExtractResponseFormat = ExtractResponseFormat.MAIN_MARKDOWN
    headers: dict[str, str] = Field(default_factory=dict, max_length=32)
    cookies: dict[str, str] = Field(default_factory=dict, max_length=64)
    max_chars: int = Field(default=20_000, ge=1, le=100_000)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("headers")
    @classmethod
    def _validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        for name, header_value in value.items():
            if name.lower() in _TRANSPORT_HEADERS:
                raise ValueError(f"{name} is managed by the HTTP transport")
            if not name or any(char in name for char in "\r\n:"):
                raise ValueError("header names must be non-empty HTTP field names")
            if "\r" in header_value or "\n" in header_value:
                raise ValueError(f"{name} contains an invalid newline")
            if len(name) > 128 or len(header_value) > 16_384:
                raise ValueError(f"{name} exceeds the target header size limit")
        return value

    @field_validator("cookies")
    @classmethod
    def _validate_cookies(cls, value: dict[str, str]) -> dict[str, str]:
        for name, cookie_value in value.items():
            if not name or any(char in name for char in "\r\n;="):
                raise ValueError("cookie names must be non-empty cookie tokens")
            if "\r" in cookie_value or "\n" in cookie_value:
                raise ValueError(f"cookie {name} contains an invalid newline")
            if len(name) > 256 or len(cookie_value) > 4096:
                raise ValueError(f"cookie {name} exceeds the size limit")
        return value

    @model_validator(mode="after")
    def _validate_start_or_continuation(self) -> Self:
        if self.cursor is None:
            if self.url is None:
                raise ValueError("url is required when cursor is absent")
        else:
            continuation_fields = self.model_fields_set - {"cursor", "max_chars"}
            if self.url is not None or continuation_fields:
                raise ValueError("cursor cannot be combined with url or extraction options")
        has_cookie_header = any(name.lower() == "cookie" for name in self.headers)
        if has_cookie_header and self.cookies:
            raise ValueError("Cookie header and cookies cannot be supplied together")
        return self


class ExtractResponse(BaseModel):
    """One bounded page of an immutable extracted document."""

    model_config = ConfigDict(extra="forbid")

    requested_url: AnyHttpUrl
    final_url: AnyHttpUrl
    status_code: int = Field(ge=100, le=599)
    title: str | None
    author: str | None
    published_at: str | None
    content_type: str
    content: str
    rendered: bool
    extractor: str
    warnings: list[str]
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    fetched_at: datetime
    truncated: bool
    next_cursor: str | None


__all__ = [
    "ExtractRequest",
    "ExtractResponse",
    "ExtractResponseFormat",
    "RenderMode",
]
