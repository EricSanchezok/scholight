"""Search-only personal access-key generation and authentication."""

from __future__ import annotations

import base64
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from cloud_auth.db.asyncpg import AsyncpgUserDatabase
from cloud_auth.models.user import UserRecord
from pydantic import BaseModel, ConfigDict

from scholight.config import settings
from scholight.db.client import get_pool
from scholight.db.queries_access_keys import (
    get_access_key_by_prefix,
    insert_access_key,
    touch_access_key_last_used,
)
from scholight.db.queries_profile import ProductAccessBlockedError, ensure_product_access

_KEY_MARKER = "sk_live_"
_LOOKUP_HEX_LENGTH = 16
_DUMMY_DIGEST = b"\0" * 32


class AccessKeyError(Exception):
    """Stable, non-sensitive access-key authentication failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AccessKeyRecord(BaseModel):
    """Stored access-key metadata; never contains the plaintext secret."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    user_id: int
    name: str
    key_prefix: str
    key_last4: str
    key_digest: bytes
    scopes: tuple[Literal["search"], ...]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None

    def require_active(self, *, now: datetime | None = None) -> None:
        current = now or datetime.now(UTC)
        if self.revoked_at is not None:
            raise AccessKeyError("access_key_revoked")
        if self.expires_at is not None and self.expires_at <= current:
            raise AccessKeyError("access_key_expired")
        if "search" not in self.scopes:
            raise AccessKeyError("invalid_access_key")


@dataclass(frozen=True, slots=True)
class GeneratedAccessKey:
    plaintext: str
    lookup_prefix: str
    last4: str
    secret_bytes: bytes


def create_access_key_secret() -> GeneratedAccessKey:
    """Create a keyed lookup prefix plus at least 256 bits of secret entropy."""
    lookup = secrets.token_hex(_LOOKUP_HEX_LENGTH // 2)
    secret_bytes = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(secret_bytes).rstrip(b"=").decode("ascii")
    lookup_prefix = f"{_KEY_MARKER}{lookup}"
    plaintext = f"{lookup_prefix}_{encoded}"
    return GeneratedAccessKey(
        plaintext=plaintext,
        lookup_prefix=lookup_prefix,
        last4=plaintext[-4:],
        secret_bytes=secret_bytes,
    )


def digest_access_key(plaintext: str, hmac_secret: str) -> bytes:
    """Return the keyed digest stored in PostgreSQL."""
    return hmac.digest(hmac_secret.encode("utf-8"), plaintext.encode("utf-8"), "sha256")


def verify_access_key_secret(plaintext: str, expected_digest: bytes, hmac_secret: str) -> bool:
    """Compare an access key with its stored digest in constant time."""
    actual = digest_access_key(plaintext, hmac_secret)
    return hmac.compare_digest(actual, expected_digest)


def access_key_lookup_prefix(plaintext: str) -> str | None:
    """Extract the non-secret lookup component from a well-formed live key."""
    if not plaintext.startswith(_KEY_MARKER):
        return None
    remainder = plaintext.removeprefix(_KEY_MARKER)
    lookup, separator, secret = remainder.partition("_")
    if (
        separator != "_"
        or len(lookup) != _LOOKUP_HEX_LENGTH
        or not secret
        or any(char not in "0123456789abcdef" for char in lookup)
    ):
        return None
    return f"{_KEY_MARKER}{lookup}"


async def resolve_access_key(plaintext: str) -> tuple[AccessKeyRecord, UserRecord]:
    """Resolve an active search key and its active owner without logging the key."""
    prefix = access_key_lookup_prefix(plaintext)
    record = await get_access_key_by_prefix(prefix) if prefix is not None else None
    expected = record.key_digest if record is not None else _DUMMY_DIGEST
    valid = verify_access_key_secret(plaintext, expected, settings.access_key_hmac_secret)
    if record is None or not valid:
        raise AccessKeyError("invalid_access_key")
    record.require_active()

    db = AsyncpgUserDatabase(pool_factory=get_pool)
    user = await db.get_user_by_id(record.user_id)
    if user is None or user.status != "active":
        raise AccessKeyError("invalid_access_key")
    try:
        await ensure_product_access(user.id)
    except ProductAccessBlockedError as exc:
        raise AccessKeyError("product_access_blocked") from exc
    await touch_access_key_last_used(record.id, record.user_id)
    return record, user


async def issue_access_key(
    *, user_id: int, name: str, expires_at: datetime | None
) -> tuple[AccessKeyRecord, str]:
    """Generate and persist a key, returning its plaintext exactly once."""
    generated = create_access_key_secret()
    record = AccessKeyRecord(
        id=uuid4(),
        user_id=user_id,
        name=name,
        key_prefix=generated.lookup_prefix,
        key_last4=generated.last4,
        key_digest=digest_access_key(generated.plaintext, settings.access_key_hmac_secret),
        scopes=("search",),
        created_at=datetime.now(UTC),
        last_used_at=None,
        expires_at=expires_at,
        revoked_at=None,
    )
    stored = await insert_access_key(record)
    return stored, generated.plaintext


__all__ = [
    "AccessKeyError",
    "AccessKeyRecord",
    "GeneratedAccessKey",
    "access_key_lookup_prefix",
    "create_access_key_secret",
    "digest_access_key",
    "issue_access_key",
    "resolve_access_key",
    "verify_access_key_secret",
]
