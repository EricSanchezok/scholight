"""Access-key generation, verification, and search-actor tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Depends, FastAPI
from sanchezcloud_identity.models.user import UserRecord

from scholight.api.access_keys import (
    AccessKeyError,
    AccessKeyRecord,
    create_access_key_secret,
    digest_access_key,
    issue_access_key,
    verify_access_key_secret,
)
from scholight.api.deps import SearchActor, get_current_user, get_optional_search_actor
from scholight.api.models.access_key import CreateAccessKeyRequest
from scholight.db.client import DBError


def _record(*, digest: bytes, expires_at: datetime | None = None) -> AccessKeyRecord:
    return AccessKeyRecord(
        id=uuid4(),
        user_id=42,
        name="literature-review",
        key_prefix="sk_live_lookup",
        key_last4="ABCD",
        key_digest=digest,
        scopes=("search",),
        created_at=datetime.now(UTC),
        last_used_at=None,
        expires_at=expires_at,
        revoked_at=None,
    )


def test_created_secret_has_live_prefix_and_32_random_bytes() -> None:
    generated = create_access_key_secret()

    assert generated.plaintext.startswith("sk_live_")
    assert len(generated.secret_bytes) == 32


def test_access_key_digest_is_hmac_and_plaintext_is_not_retained() -> None:
    generated = create_access_key_secret()

    digest = digest_access_key(generated.plaintext, "h" * 32)

    assert len(digest) == 32
    assert generated.plaintext.encode() not in digest


def test_access_key_verification_uses_digest_match() -> None:
    generated = create_access_key_secret()
    digest = digest_access_key(generated.plaintext, "h" * 32)

    assert verify_access_key_secret(generated.plaintext, digest, "h" * 32)


def test_access_key_verification_rejects_wrong_secret() -> None:
    generated = create_access_key_secret()
    digest = digest_access_key(generated.plaintext, "h" * 32)

    assert not verify_access_key_secret(f"{generated.plaintext}wrong", digest, "h" * 32)


@pytest.mark.asyncio
async def test_access_key_search_actor_is_attributed_to_owner(active_user: UserRecord) -> None:
    key_id = uuid4()
    record = _record(digest=b"d" * 32)
    record = record.model_copy(update={"id": key_id})
    app = FastAPI()

    @app.get("/")
    async def identity(
        actor: SearchActor | None = Depends(get_optional_search_actor),
    ) -> dict[str, str]:
        assert actor is not None
        return {"actor_type": actor.actor_type, "access_key_id": str(actor.access_key_id)}

    with (
        patch(
            "scholight.api.deps.resolve_access_key",
            AsyncMock(return_value=(record, active_user)),
        ),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/", headers={"Authorization": "Bearer sk_live_value"})

    assert response.json() == {"actor_type": "access_key", "access_key_id": str(key_id)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status_code", "code", "message"),
    [
        (
            AccessKeyError("invalid_access_key"),
            401,
            "invalid_access_key",
            "Access key is invalid.",
        ),
        (
            AccessKeyError("access_key_revoked"),
            401,
            "access_key_revoked",
            "Access key has been revoked.",
        ),
        (
            AccessKeyError("access_key_expired"),
            401,
            "access_key_expired",
            "Access key has expired.",
        ),
        (
            AccessKeyError("product_access_blocked"),
            403,
            "product_access_blocked",
            "Scholight access for this account is blocked.",
        ),
    ],
)
async def test_invalid_access_key_never_falls_back_to_jwt(
    error: AccessKeyError,
    status_code: int,
    code: str,
    message: str,
) -> None:
    app = FastAPI()

    @app.get("/")
    async def identity(_actor: SearchActor | None = Depends(get_optional_search_actor)) -> None:
        return None

    with (
        patch("scholight.api.deps.resolve_access_key", AsyncMock(side_effect=error)),
        patch("scholight.api.deps._get_current_user_callable", AsyncMock()) as jwt_resolver,
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/", headers={"Authorization": "Bearer sk_live_value"})

    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == code
    assert response.json()["detail"]["message"] == message
    jwt_resolver.assert_not_awaited()


def test_expired_record_is_rejected() -> None:
    record = _record(digest=b"d" * 32, expires_at=datetime.now(UTC) - timedelta(seconds=1))

    with pytest.raises(AccessKeyError, match="access_key_expired"):
        record.require_active()


def test_access_key_record_id_is_uuid() -> None:
    record = _record(digest=b"d" * 32)

    assert isinstance(record.id, UUID)


@pytest.mark.asyncio
async def test_issue_access_key_returns_plaintext_once_without_persisting_it() -> None:
    captured: dict[str, object] = {}

    async def insert(record: AccessKeyRecord) -> AccessKeyRecord:
        captured.update(record.model_dump())
        return record

    with patch("scholight.api.access_keys.insert_access_key", side_effect=insert):
        created, plaintext = await issue_access_key(
            user_id=42,
            name="literature-review",
            expires_at=None,
        )

    assert plaintext.startswith("sk_live_")
    assert created.key_last4 == plaintext[-4:]
    assert plaintext not in repr(captured)
    assert "key_digest" in captured


def test_access_key_list_schema_never_contains_secret_or_digest(api_app: FastAPI) -> None:
    schema = api_app.openapi()
    operation = schema["paths"]["/user/access-keys"]["get"]
    response_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]["items"][
        "$ref"
    ]
    model = schema["components"]["schemas"][response_ref.rsplit("/", 1)[-1]]

    assert "key" not in model["properties"]
    assert "key_digest" not in model["properties"]


@pytest.mark.asyncio
async def test_access_key_database_failure_is_explicitly_retryable(
    api_app: FastAPI,
    active_user: UserRecord,
) -> None:
    api_app.dependency_overrides[get_current_user] = lambda: active_user
    with patch(
        "scholight.api.routes.access_keys.issue_access_key",
        AsyncMock(side_effect=DBError("private SQL detail")),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/user/access-keys",
                json=CreateAccessKeyRequest(name="automation").model_dump(mode="json"),
            )

    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json() == {
        "detail": {
            "code": "access_key_service_unavailable",
            "message": "Access key management is temporarily unavailable.",
            "retryable": True,
        }
    }
