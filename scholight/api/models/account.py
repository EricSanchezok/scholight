"""Account lifecycle request models."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class DeleteAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    password: str = Field(min_length=1, max_length=1024)
    confirmation: str = Field(max_length=16)


__all__ = ["DeleteAccountRequest"]
