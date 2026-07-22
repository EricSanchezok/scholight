"""Strict public request and response models for personal access keys."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scholight.api.access_keys import AccessKeyRecord


def _default_search_scope() -> list[Literal["search"]]:
    return ["search"]


class CreateAccessKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    scopes: list[Literal["search"]] = Field(default_factory=_default_search_scope)
    expires_at: datetime | None = None

    @field_validator("name")
    @staticmethod
    def _normalize_name(value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("scopes")
    @staticmethod
    def _search_scope_only(value: list[str]) -> list[str]:
        if value != ["search"]:
            raise ValueError("only the search scope is supported")
        return value

    @field_validator("expires_at")
    @staticmethod
    def _future_expiry(value: datetime | None) -> datetime | None:
        if value is not None and value <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return value


class UpdateAccessKeyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=64)
    expires_at: datetime | None = None

    @field_validator("name")
    @staticmethod
    def _normalize_name(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("name must not be blank")
        return normalized

    @field_validator("expires_at")
    @staticmethod
    def _future_expiry(value: datetime | None) -> datetime | None:
        if value is not None and value <= datetime.now(UTC):
            raise ValueError("expires_at must be in the future")
        return value

    @model_validator(mode="after")
    def _at_least_one_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class AccessKeyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    name: str
    prefix: str
    last4: str
    scopes: list[Literal["search"]]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def from_record(cls, record: AccessKeyRecord) -> Self:
        return cls(
            id=record.id,
            name=record.name,
            prefix=record.key_prefix,
            last4=record.key_last4,
            scopes=list(record.scopes),
            created_at=record.created_at,
            last_used_at=record.last_used_at,
            expires_at=record.expires_at,
            revoked_at=record.revoked_at,
        )


class CreatedAccessKeyResponse(AccessKeyResponse):
    key: str


__all__ = [
    "AccessKeyResponse",
    "CreateAccessKeyRequest",
    "CreatedAccessKeyResponse",
    "UpdateAccessKeyRequest",
]
