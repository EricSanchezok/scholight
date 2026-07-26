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

from scholight.api.access_keys import AccessKeyError, access_key_error_message
from scholight.api.deps import (
    DelegationError,
    resolve_access_key_search_actor,
    resolve_delegated_search_actor,
)
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
    """Apply exact Origin checks and search-scoped MCP identity."""

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
            if separator != " " or scheme.lower() != "bearer" or not token:
                response = _error_response(
                    401,
                    code="invalid_access_key",
                    message="Access key is invalid.",
                    retryable=False,
                )
                await response(scope, receive, send)
                return
            try:
                actor = (
                    await resolve_access_key_search_actor(token)
                    if token.startswith("sk_live_")
                    else await resolve_delegated_search_actor(token)
                )
            except AccessKeyError as exc:
                status_code = 403 if exc.code == "product_access_blocked" else 401
                response = _error_response(
                    status_code,
                    code=exc.code,
                    message=access_key_error_message(exc.code),
                    retryable=False,
                )
                await response(scope, receive, send)
                return
            except DelegationError as exc:
                response = _error_response(
                    exc.status_code,
                    code=exc.code,
                    message="Delegated identity is invalid or unavailable.",
                    retryable=exc.status_code == 503,
                    retry_after=5 if exc.status_code == 503 else None,
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
            SearchInvocation(
                actor=actor,
                client_ip=client_ip,
                request_id=request_id,
                transport="mcp",
            )
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
        lines.extend(["", f"## {hit.rank}. [{hit.title}]({hit.arxiv_url})"])
        if hit.authors:
            lines.append(f"**Authors:** {', '.join(hit.authors)}")
        metadata = [f"**arXiv:** {hit.arxiv_id}"]
        if hit.categories:
            metadata.append(f"**Categories:** {', '.join(hit.categories)}")
        lines.append(" · ".join(metadata))
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
    text = f"{exc.code}: {exc.message}"
    if exc.quota is not None:
        quota = exc.quota.model_dump(mode="json")
        error["quota"] = quota
        strength = exc.quota.strength.title()
        window = "daily" if exc.quota.window == "day" else "minute"
        text = (
            f"{text} {strength} {window} quota exhausted "
            f"({exc.quota.used}/{exc.quota.limit} used); "
            f"resets at {quota['reset_at']}."
        )
    return CallToolResult(
        isError=True,
        content=[TextContent(type="text", text=text)],
        structuredContent={"error": error},
    )


async def search_papers(
    query: Annotated[
        str,
        Field(
            min_length=1,
            max_length=500,
            description=(
                "A focused natural-language research question or topic. Include the task, method, "
                "domain, or comparison that matters; avoid a loose list of unrelated keywords."
            ),
        ),
    ],
    strength: Annotated[
        Literal["standard", "thorough"],
        Field(
            description=(
                "Search depth. Use standard by default for fast, iterative discovery. Use thorough "
                "when the question is nuanced and deeper ranking justifies higher latency and "
                "consumption of the separate Thorough quota."
            )
        ),
    ] = "standard",
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=20,
            description=(
                "Maximum number of ranked papers to return. Use 5 for a focused answer and increase "
                "only when the user needs broader coverage."
            ),
        ),
    ] = 5,
    categories: Annotated[
        list[str] | None,
        Field(
            max_length=10,
            description=(
                "Optional exact subject category codes such as cs.AI or cs.CL. Multiple values match "
                "papers in any listed category. Use only when requested or known with confidence."
            ),
        ),
    ] = None,
    authors: Annotated[
        list[str] | None,
        Field(
            max_length=10,
            description=(
                "Optional exact author names. Multiple values match papers containing any listed "
                "author. Use only when the user asks for specific authors; otherwise omit."
            ),
        ),
    ] = None,
    date_from: Annotated[
        date | None,
        Field(
            description=(
                "Optional inclusive earliest submission date in YYYY-MM-DD format. Omit when the "
                "user did not request a lower time boundary."
            )
        ),
    ] = None,
    date_to: Annotated[
        date | None,
        Field(
            description=(
                "Optional inclusive latest submission date in YYYY-MM-DD format. Omit when the user "
                "did not request an upper time boundary."
            )
        ),
    ] = None,
) -> CallToolResult:
    """Search Scholight for ranked AI research papers."""
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
        instructions=(
            "Use Scholight to find and compare AI research papers. "
            "Call search_papers for literature discovery, related-work research, method comparisons, "
            "or author, category, and date-filtered paper searches. Prefer standard for most requests; "
            "use thorough when nuanced queries benefit from deeper ranking despite higher latency and "
            "consumption of the separate Thorough quota."
        ),
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    server.tool(
        name="search_papers",
        description=(
            "Find ranked AI research papers relevant to a natural-language question or topic. Use this "
            "tool for literature discovery, related-work research, method comparisons, and author, "
            "category, or date-filtered research. Results include titles, authors, abstracts, dates, "
            "categories, and paper and PDF links. Preserve the returned rank order. Use standard by "
            "default; use thorough for nuanced queries when deeper ranking justifies higher latency and "
            "consumption of the separate Thorough quota."
        ),
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
