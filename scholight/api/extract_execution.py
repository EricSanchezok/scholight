"""Transport-neutral orchestration for the public Web Extract contract."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from scholight.api.deps import SearchActor
from scholight.config import settings
from scholight.models.web_extract import ExtractRequest, ExtractResponse
from scholight.web_extract.cache import ExtractResultCache, PageSlice
from scholight.web_extract.service import InternalExtractResponse


@dataclass(frozen=True, slots=True)
class ExtractInvocation:
    actor: SearchActor | None
    request_id: str
    transport: Literal["rest", "mcp"]


class PublicExtractError(Exception):
    """Stable extraction failure shared by the REST and MCP adapters."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after

    @property
    def http_detail(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }


def _new_cache() -> ExtractResultCache:
    return ExtractResultCache(
        ttl_seconds=settings.extract_cache_ttl_seconds,
        max_bytes=settings.extract_cache_max_bytes,
    )


_result_cache = _new_cache()


def reset_extract_result_cache() -> None:
    """Reset process-local private cursor state at startup and in tests."""
    global _result_cache
    _result_cache = _new_cache()


def _actor_key(actor: SearchActor) -> str:
    key_id = str(actor.access_key_id) if actor.access_key_id is not None else "none"
    return f"{actor.actor_type}:{actor.user.id}:{key_id}"


def _public_error_from_response(response: httpx.Response) -> PublicExtractError:
    if response.status_code == 401:
        return PublicExtractError(
            status_code=503,
            code="extract_service_unavailable",
            message="Web extraction is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        )
    try:
        detail = response.json()["detail"]
        return PublicExtractError(
            status_code=response.status_code,
            code=str(detail["code"]),
            message=str(detail["message"]),
            retryable=bool(detail["retryable"]),
            retry_after=5 if response.status_code == 503 else None,
        )
    except (KeyError, TypeError, ValueError):
        return PublicExtractError(
            status_code=502,
            code="extract_service_invalid_response",
            message="Web extraction returned an invalid response.",
            retryable=True,
            retry_after=5,
        )


async def _request_document(request: ExtractRequest) -> InternalExtractResponse:
    payload = request.model_dump(
        mode="json",
        include={"url", "render", "output", "headers", "cookies"},
    )
    try:
        async with httpx.AsyncClient(
            timeout=settings.extract_request_timeout_seconds,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"{settings.extract_service_url.rstrip('/')}/v1/extract",
                headers={"X-Scholight-Internal-Token": settings.extract_internal_token},
                json=payload,
            )
    except httpx.TimeoutException as exc:
        raise PublicExtractError(
            status_code=504,
            code="extract_timeout",
            message="Web extraction exceeded the request deadline.",
            retryable=True,
        ) from exc
    except httpx.HTTPError as exc:
        raise PublicExtractError(
            status_code=503,
            code="extract_service_unavailable",
            message="Web extraction is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ) from exc
    if response.status_code >= 400:
        raise _public_error_from_response(response)
    try:
        return InternalExtractResponse.model_validate(response.json())
    except (ValueError, TypeError) as exc:
        raise PublicExtractError(
            status_code=502,
            code="extract_service_invalid_response",
            message="Web extraction returned an invalid response.",
            retryable=True,
            retry_after=5,
        ) from exc


def _page_response(page: PageSlice) -> ExtractResponse:
    return ExtractResponse.model_validate(
        {
            **page.metadata,
            "content": page.content,
            "truncated": page.next_cursor is not None,
            "next_cursor": page.next_cursor,
        }
    )


async def execute_public_extract(
    request: ExtractRequest,
    invocation: ExtractInvocation,
) -> ExtractResponse:
    actor = invocation.actor
    if actor is None:
        raise PublicExtractError(
            status_code=401,
            code="authentication_required",
            message="Use a Scholight Access Key to call Web Extract.",
            retryable=False,
        )
    actor_key = _actor_key(actor)
    if request.cursor is not None:
        page = _result_cache.read(
            request.cursor,
            actor_key=actor_key,
            max_chars=request.max_chars,
        )
        if page is None:
            raise PublicExtractError(
                status_code=400,
                code="invalid_cursor",
                message="Cursor is invalid, expired, or belongs to another identity.",
                retryable=False,
            )
        return _page_response(page)

    document = await _request_document(request)
    metadata = document.model_dump(mode="json", exclude={"content"})
    if len(document.content) <= request.max_chars:
        return ExtractResponse.model_validate(
            {
                **metadata,
                "content": document.content,
                "truncated": False,
                "next_cursor": None,
            }
        )
    cursor = _result_cache.put_private(
        actor_key=actor_key,
        url=document.final_url,
        content=document.content,
        metadata=metadata,
    )
    page = _result_cache.read(cursor, actor_key=actor_key, max_chars=request.max_chars)
    if page is None:
        raise PublicExtractError(
            status_code=503,
            code="extract_cursor_unavailable",
            message="Extracted content could not be paged.",
            retryable=True,
        )
    return _page_response(page)


__all__ = [
    "ExtractInvocation",
    "PublicExtractError",
    "execute_public_extract",
    "reset_extract_result_cache",
]
