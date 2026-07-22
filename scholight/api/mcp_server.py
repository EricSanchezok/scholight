"""Stateless MCP boundary exposing Scholight public search."""

from __future__ import annotations

from contextvars import ContextVar
from datetime import date
from typing import Annotated, Any, Literal, cast
from uuid import uuid4

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send
from structlog.contextvars import get_contextvars

from scholight.api.access_keys import AccessKeyError
from scholight.api.deps import resolve_access_key_search_actor
from scholight.api.models.search import (
    PublicSearchFilters,
    PublicSearchRequest,
    PublicSearchResponse,
    SearchStrength,
)
from scholight.api.search_execution import (
    PublicSearchError,
    SearchInvocation,
    execute_public_search,
)
from scholight.config import settings
from scholight.db.client import DBError

_current_invocation: ContextVar[SearchInvocation | None] = ContextVar(
    "scholight_mcp_invocation",
    default=None,
)


def _header(scope: Scope, name: bytes) -> str | None:
    headers = cast("list[tuple[bytes, bytes]]", scope.get("headers", []))
    for key, value in headers:
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _error_response(
    status_code: int,
    *,
    code: str,
    message: str,
    retryable: bool,
    retry_after: int | None = None,
) -> JSONResponse:
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else None
    if status_code == 401:
        headers = {**(headers or {}), "WWW-Authenticate": "Bearer"}
    return JSONResponse(
        status_code=status_code,
        content={
            "detail": {
                "code": code,
                "message": message,
                "retryable": retryable,
            }
        },
        headers=headers,
    )


class _MCPRequestBoundary:
    """Apply exact Origin checks and access-key-only identity to MCP requests."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        origin = _header(scope, b"origin")
        if origin is not None and origin not in settings.cors_allow_origins:
            response = _error_response(
                403,
                code="origin_not_allowed",
                message="Origin is not allowed.",
                retryable=False,
            )
            await response(scope, receive, send)
            return

        actor = None
        authorization = _header(scope, b"authorization")
        if authorization is not None:
            scheme, separator, token = authorization.partition(" ")
            if (
                separator != " "
                or scheme.lower() != "bearer"
                or not token
                or not token.startswith("sk_live_")
            ):
                response = _error_response(
                    401,
                    code="invalid_access_key",
                    message="Access key is invalid or unavailable.",
                    retryable=False,
                )
                await response(scope, receive, send)
                return
            try:
                actor = await resolve_access_key_search_actor(token)
            except AccessKeyError as exc:
                response = _error_response(
                    401,
                    code=exc.code,
                    message="Access key is invalid or unavailable.",
                    retryable=False,
                )
                await response(scope, receive, send)
                return
            except DBError:
                response = _error_response(
                    503,
                    code="access_key_service_unavailable",
                    message="Access key service is temporarily unavailable.",
                    retryable=True,
                    retry_after=5,
                )
                await response(scope, receive, send)
                return

        client = scope.get("client")
        client_ip = client[0] if client is not None else None
        request_id = str(get_contextvars().get("request_id") or uuid4())
        context = _current_invocation.set(
            SearchInvocation(actor=actor, client_ip=client_ip, request_id=request_id)
        )
        try:
            await self._app(scope, receive, send)
        finally:
            _current_invocation.reset(context)


def _invocation() -> SearchInvocation:
    invocation = _current_invocation.get()
    if invocation is None:
        raise RuntimeError("MCP search called without request context")
    return invocation


def _trim_abstract(value: str | None, *, limit: int = 600) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return f"{value[: limit - 1].rstrip()}…"


def _format_markdown(response: PublicSearchResponse) -> str:
    lines = [
        f"# Scholight results for: {response.query}",
        "",
        f"Found {response.result_count} paper(s) with {response.strength.value} search.",
    ]
    if response.degraded:
        lines.extend(["", "Some paper metadata could not be enriched."])
    for hit in response.hits:
        lines.extend(
            [
                "",
                f"## {hit.rank}. [{hit.title}]({hit.arxiv_url})",
                f"**Authors:** {', '.join(hit.authors)}",
                f"**arXiv:** {hit.arxiv_id} · **Categories:** {', '.join(hit.categories)}",
            ]
        )
        abstract = _trim_abstract(hit.abstract)
        if abstract is not None:
            lines.extend(["", abstract])
    return "\n".join(lines)


def _tool_error(exc: PublicSearchError) -> CallToolResult:
    error: dict[str, Any] = {
        "code": exc.code,
        "message": exc.message,
        "retryable": exc.retryable,
    }
    if exc.retry_after is not None:
        error["retry_after"] = exc.retry_after
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=f"{exc.code}: {exc.message}")],
        structuredContent={"error": error},
    )


async def search_papers(
    query: Annotated[str, Field(min_length=1, max_length=500)],
    strength: Literal["standard", "thorough"] = "standard",
    limit: Annotated[int, Field(ge=1, le=20)] = 5,
    categories: Annotated[list[str] | None, Field(max_length=10)] = None,
    authors: Annotated[list[str] | None, Field(max_length=10)] = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> CallToolResult:
    """Search papers using Standard for speed or Thorough for deeper ranking."""
    request = PublicSearchRequest(
        query=query,
        strength=SearchStrength(strength),
        limit=limit,
        filters=PublicSearchFilters(
            categories=categories or [],
            authors=authors or [],
            date_from=date_from,
            date_to=date_to,
        ),
    )
    try:
        response = await execute_public_search(request, _invocation())
    except PublicSearchError as exc:
        return _tool_error(exc)
    return CallToolResult(
        isError=False,
        content=[TextContent(type="text", text=_format_markdown(response))],
        structuredContent=response.model_dump(mode="json"),
    )


def create_mcp_app() -> tuple[FastMCP[Any], ASGIApp]:
    """Create a fresh MCP server and ASGI app for one FastAPI application."""
    server = FastMCP(
        name="Scholight",
        instructions="Search AI research papers from Scholight's arXiv index.",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    server.tool(
        name="search_papers",
        description="Search arXiv papers indexed by Scholight and return ranked paper metadata.",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )(search_papers)
    app: ASGIApp = _MCPRequestBoundary(server.streamable_http_app())
    return server, app


__all__ = ["create_mcp_app"]
