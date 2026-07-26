"""Scholight MCP Streamable HTTP contract tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
import jwt
import pytest
import pytest_asyncio
from cloud_auth.models.user import UserRecord
from pydantic import AnyHttpUrl

from scholight.api.access_keys import AccessKeyError
from scholight.api.app import create_app
from scholight.api.deps import (
    DelegationError,
    SearchActor,
    resolve_delegated_search_actor,
)
from scholight.api.models.search import (
    PublicSearchHit,
    PublicSearchRequest,
    PublicSearchResponse,
    SearchStrength,
)
from scholight.api.search_access import reset_anonymous_minute_limits
from scholight.api.search_execution import PublicSearchError, SearchInvocation
from scholight.config import settings
from scholight.db.queries_anonymous_quota import AnonymousQuotaReservation
from scholight.models.search import SearchResult

pytestmark = pytest.mark.asyncio(loop_scope="module")

_MCP_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "mcp-protocol-version": "2025-11-25",
}


def _request(method: str, *, request_id: int, params: dict[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params,
    }


async def _initialize(client: httpx.AsyncClient, *, request_id: int = 1) -> httpx.Response:
    return await client.post(
        "/mcp",
        headers=_MCP_HEADERS,
        json=_request(
            "initialize",
            request_id=request_id,
            params={
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "1.0"},
            },
        ),
    )


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def mcp_client() -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(settings, "auth_jwt_secret", "j" * 32)
    monkeypatch.setattr(settings, "jwt_secret", "j" * 32)
    monkeypatch.setattr(settings, "anonymous_quota_hmac_secret", "h" * 32)
    monkeypatch.setattr(settings, "access_key_hmac_secret", "k" * 32)
    monkeypatch.setattr(settings, "mcp_delegation_jwt_secret", "d" * 32)
    monkeypatch.setattr(settings, "zilliz_uri", "https://zilliz.example.invalid")
    monkeypatch.setattr(settings, "zilliz_token", "fixture-token")
    monkeypatch.setattr(settings, "embedding_base_url", "https://embedding.example.invalid/v1")
    monkeypatch.setattr(settings, "proxy_headers", False)
    monkeypatch.setattr(settings, "forwarded_allow_ips", "127.0.0.1")
    monkeypatch.setattr(settings, "cors_allow_origins", ["http://localhost:3000"])
    reset_anonymous_minute_limits()
    app = create_app()
    mcp_server = app.state.mcp_server
    transport = httpx.ASGITransport(app=app, client=("192.0.2.50", 12345))
    ready = asyncio.Event()
    stop = asyncio.Event()

    async def run_session_manager() -> None:
        async with mcp_server.session_manager.run():
            ready.set()
            await stop.wait()

    manager_task = asyncio.create_task(run_session_manager())
    await ready.wait()
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as client:
            yield client
    finally:
        stop.set()
        await manager_task
        reset_anonymous_minute_limits()
        monkeypatch.undo()


async def test_initialize_and_list_tools_do_not_execute_search(
    mcp_client: httpx.AsyncClient,
) -> None:
    with patch("scholight.api.mcp_server.execute_public_search", new_callable=AsyncMock) as execute:
        initialized = await _initialize(mcp_client)
        listed = await mcp_client.post(
            "/mcp",
            headers=_MCP_HEADERS,
            json=_request("tools/list", request_id=2, params={}),
        )

    assert initialized.status_code == 200
    assert initialized.json()["result"]["protocolVersion"] == "2025-11-25"
    assert initialized.json()["result"]["instructions"] == (
        "Use Scholight to find and compare AI research papers. "
        "Call search_papers for literature discovery, related-work research, method comparisons, "
        "or author, category, and date-filtered paper searches. Prefer standard for most requests; "
        "use thorough when nuanced queries benefit from deeper ranking despite higher latency and "
        "consumption of the separate Thorough quota."
    )
    assert listed.status_code == 200
    tools = listed.json()["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["search_papers"]
    assert tools[0]["description"] == (
        "Find ranked AI research papers relevant to a natural-language question or topic. Use this "
        "tool for literature discovery, related-work research, method comparisons, and author, "
        "category, or date-filtered research. Results include titles, authors, abstracts, dates, "
        "categories, and paper and PDF links. Preserve the returned rank order. Use standard by "
        "default; use thorough for nuanced queries when deeper ranking justifies higher latency and "
        "consumption of the separate Thorough quota."
    )
    properties = tools[0]["inputSchema"]["properties"]
    assert {name: properties[name]["description"] for name in properties} == {
        "query": (
            "A focused natural-language research question or topic. Include the task, method, "
            "domain, or comparison that matters; avoid a loose list of unrelated keywords."
        ),
        "strength": (
            "Search depth. Use standard by default for fast, iterative discovery. Use thorough "
            "when the question is nuanced and deeper ranking justifies higher latency and "
            "consumption of the separate Thorough quota."
        ),
        "limit": (
            "Maximum number of ranked papers to return. Use 5 for a focused answer and increase "
            "only when the user needs broader coverage."
        ),
        "categories": (
            "Optional exact subject category codes such as cs.AI or cs.CL. Multiple values match "
            "papers in any listed category. Use only when requested or known with confidence."
        ),
        "authors": (
            "Optional exact author names. Multiple values match papers containing any listed "
            "author. Use only when the user asks for specific authors; otherwise omit."
        ),
        "date_from": (
            "Optional inclusive earliest submission date in YYYY-MM-DD format. Omit when the user "
            "did not request a lower time boundary."
        ),
        "date_to": (
            "Optional inclusive latest submission date in YYYY-MM-DD format. Omit when the user "
            "did not request an upper time boundary."
        ),
    }
    assert tools[0]["annotations"]["readOnlyHint"] is True
    assert tools[0]["annotations"]["destructiveHint"] is False
    execute.assert_not_awaited()


async def test_anonymous_tool_call_returns_markdown_and_public_response_structure(
    mcp_client: httpx.AsyncClient,
) -> None:
    response = PublicSearchResponse(
        query="retrieval",
        strength=SearchStrength.STANDARD,
        degraded=False,
        hits=[
            PublicSearchHit(
                rank=1,
                score=1.0,
                arxiv_id="2401.12345",
                title="Sparse metadata paper",
                authors=[],
                abstract=None,
                categories=[],
                submitted_at=None,
                updated_at=None,
                version=None,
                arxiv_url=AnyHttpUrl("https://arxiv.org/abs/2401.12345"),
                pdf_url=AnyHttpUrl("https://arxiv.org/pdf/2401.12345"),
            )
        ],
        result_count=1,
        elapsed_ms=1.25,
    )
    with patch(
        "scholight.api.mcp_server.execute_public_search",
        new_callable=AsyncMock,
        return_value=response,
    ) as execute:
        called = await mcp_client.post(
            "/mcp",
            headers=_MCP_HEADERS,
            json=_request(
                "tools/call",
                request_id=3,
                params={
                    "name": "search_papers",
                    "arguments": {"query": "retrieval", "limit": 5},
                },
            ),
        )

    assert called.status_code == 200
    result = called.json()["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == response.model_dump(mode="json")
    markdown = result["content"][0]["text"]
    assert "retrieval" in markdown
    assert "**arXiv:** 2401.12345" in markdown
    assert "**Authors:**" not in markdown
    assert "**Categories:**" not in markdown
    execute_call = execute.await_args
    assert execute_call is not None
    invocation = execute_call.args[1]
    assert (invocation.actor, invocation.client_ip) == (None, "192.0.2.50")


async def test_access_key_is_resolved_as_search_actor(
    mcp_client: httpx.AsyncClient,
    active_user: UserRecord,
) -> None:
    actor = SearchActor(
        user=active_user,
        actor_type="access_key",
        access_key_id=uuid4(),
    )
    response = PublicSearchResponse(
        query="retrieval",
        strength=SearchStrength.STANDARD,
        degraded=False,
        hits=[],
        result_count=0,
        elapsed_ms=1.0,
    )
    headers = {**_MCP_HEADERS, "authorization": "Bearer sk_live_0123456789abcdef_secret"}
    with (
        patch(
            "scholight.api.mcp_server.resolve_access_key_search_actor",
            new_callable=AsyncMock,
            return_value=actor,
        ) as resolve,
        patch(
            "scholight.api.mcp_server.execute_public_search",
            new_callable=AsyncMock,
            return_value=response,
        ) as execute,
    ):
        called = await mcp_client.post(
            "/mcp",
            headers=headers,
            json=_request(
                "tools/call",
                request_id=4,
                params={"name": "search_papers", "arguments": {"query": "retrieval"}},
            ),
        )

    assert called.status_code == 200
    resolve.assert_awaited_once_with("sk_live_0123456789abcdef_secret")
    execute_call = execute.await_args
    assert execute_call is not None
    assert (execute_call.args[1].actor, execute_call.args[1].transport) == (actor, "mcp")


async def test_delegation_jwt_is_resolved_as_current_user(
    mcp_client: httpx.AsyncClient,
    active_user: UserRecord,
) -> None:
    actor = SearchActor(user=active_user, actor_type="delegated")
    response = PublicSearchResponse(
        query="retrieval",
        strength=SearchStrength.STANDARD,
        degraded=False,
        hits=[],
        result_count=0,
        elapsed_ms=1.0,
    )
    token = jwt.encode(
        {
            "iss": "scholens",
            "aud": "scholight-mcp",
            "sub": str(active_user.id),
            "scope": "search",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int(datetime.now(UTC).timestamp()) + 60,
            "jti": str(uuid4()),
        },
        "d" * 32,
        algorithm="HS256",
    )
    with (
        patch(
            "scholight.api.mcp_server.resolve_delegated_search_actor",
            new_callable=AsyncMock,
            return_value=actor,
        ) as resolve,
        patch(
            "scholight.api.mcp_server.execute_public_search",
            new_callable=AsyncMock,
            return_value=response,
        ) as execute,
    ):
        called = await mcp_client.post(
            "/mcp",
            headers={**_MCP_HEADERS, "authorization": f"Bearer {token}"},
            json=_request(
                "tools/call",
                request_id=41,
                params={"name": "search_papers", "arguments": {"query": "retrieval"}},
            ),
        )

    assert called.status_code == 200
    resolve.assert_awaited_once_with(token)
    execute_call = execute.await_args
    assert execute_call is not None
    assert execute_call.args[1].actor is actor


async def test_delegation_rejects_wrong_audience() -> None:
    token = jwt.encode(
        {
            "iss": "scholens",
            "aud": "wrong",
            "sub": "42",
            "scope": "search",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int(datetime.now(UTC).timestamp()) + 60,
            "jti": str(uuid4()),
        },
        "d" * 32,
        algorithm="HS256",
    )
    with pytest.raises(DelegationError, match="invalid_delegation"):
        await resolve_delegated_search_actor(token)


@pytest.mark.parametrize("authorization", ["Basic abc", "Bearer"])
async def test_mcp_rejects_non_access_key_credentials(
    mcp_client: httpx.AsyncClient,
    authorization: str,
) -> None:
    response = await mcp_client.post(
        "/mcp",
        headers={**_MCP_HEADERS, "authorization": authorization},
        json=_request("tools/list", request_id=5, params={}),
    )

    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "invalid_access_key"


@pytest.mark.parametrize(
    ("error_code", "status_code", "message"),
    [
        ("access_key_revoked", 401, "Access key has been revoked."),
        ("access_key_expired", 401, "Access key has expired."),
        (
            "product_access_blocked",
            403,
            "Scholight access for this account is blocked.",
        ),
    ],
)
async def test_mcp_reports_access_key_state(
    mcp_client: httpx.AsyncClient,
    error_code: str,
    status_code: int,
    message: str,
) -> None:
    with patch(
        "scholight.api.mcp_server.resolve_access_key_search_actor",
        new_callable=AsyncMock,
        side_effect=AccessKeyError(error_code),
    ):
        response = await mcp_client.post(
            "/mcp",
            headers={
                **_MCP_HEADERS,
                "authorization": "Bearer sk_live_0123456789abcdef_secret",
            },
            json=_request("tools/list", request_id=6, params={}),
        )

    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == error_code
    assert response.json()["detail"]["message"] == message


async def test_mcp_origin_is_optional_but_exact_when_present(
    mcp_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "cors_allow_origins", ["https://app.example.com"])

    missing = await _initialize(mcp_client, request_id=7)
    allowed = await mcp_client.post(
        "/mcp",
        headers={**_MCP_HEADERS, "origin": "https://app.example.com"},
        json=_request("tools/list", request_id=8, params={}),
    )
    denied = await mcp_client.post(
        "/mcp",
        headers={**_MCP_HEADERS, "origin": "https://evil.example.com"},
        json=_request("tools/list", request_id=9, params={}),
    )

    assert (missing.status_code, allowed.status_code, denied.status_code) == (200, 200, 403)


@pytest.mark.parametrize(
    ("status_code", "code"),
    [
        (429, "anonymous_rate_limit_exceeded"),
        (503, "search_unavailable"),
    ],
)
async def test_search_failures_become_mcp_tool_errors(
    mcp_client: httpx.AsyncClient,
    status_code: int,
    code: str,
) -> None:
    with patch(
        "scholight.api.mcp_server.execute_public_search",
        new_callable=AsyncMock,
        side_effect=PublicSearchError(
            status_code=status_code,
            code=code,
            message="Search is temporarily unavailable.",
            retryable=True,
            retry_after=5,
        ),
    ):
        called = await mcp_client.post(
            "/mcp",
            headers=_MCP_HEADERS,
            json=_request(
                "tools/call",
                request_id=10,
                params={"name": "search_papers", "arguments": {"query": "retrieval"}},
            ),
        )

    result = called.json()["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == code


async def test_quota_failure_gives_agents_machine_and_human_context(
    mcp_client: httpx.AsyncClient,
) -> None:
    with patch(
        "scholight.api.mcp_server.execute_public_search",
        new_callable=AsyncMock,
        side_effect=PublicSearchError(
            status_code=429,
            code="user_daily_quota_exceeded",
            message="Daily search quota exceeded.",
            retryable=True,
            retry_after=3600,
            quota={
                "scope": "user",
                "strength": "standard",
                "window": "day",
                "limit": 1000,
                "used": 1000,
                "remaining": 0,
                "reset_at": "2026-07-24T00:00:00Z",
            },
        ),
    ):
        called = await mcp_client.post(
            "/mcp",
            headers=_MCP_HEADERS,
            json=_request(
                "tools/call",
                request_id=17,
                params={
                    "name": "search_papers",
                    "arguments": {"query": "retrieval", "strength": "standard"},
                },
            ),
        )

    error = called.json()["result"]
    assert error == {
        "content": [
            {
                "type": "text",
                "text": (
                    "user_daily_quota_exceeded: Daily search quota exceeded. "
                    "Standard daily quota exhausted (1000/1000 used); "
                    "resets at 2026-07-24T00:00:00Z."
                ),
            }
        ],
        "structuredContent": {
            "error": {
                "code": "user_daily_quota_exceeded",
                "message": "Daily search quota exceeded.",
                "retryable": True,
                "retry_after": 3600,
                "quota": {
                    "scope": "user",
                    "strength": "standard",
                    "window": "day",
                    "limit": 1000,
                    "used": 1000,
                    "remaining": 0,
                    "reset_at": "2026-07-24T00:00:00Z",
                },
            }
        },
        "isError": True,
    }


async def test_invalid_tool_input_does_not_execute_search(mcp_client: httpx.AsyncClient) -> None:
    with patch("scholight.api.mcp_server.execute_public_search", new_callable=AsyncMock) as execute:
        called = await mcp_client.post(
            "/mcp",
            headers=_MCP_HEADERS,
            json=_request(
                "tools/call",
                request_id=11,
                params={"name": "search_papers", "arguments": {"query": " ", "limit": 21}},
            ),
        )

    assert called.status_code == 200
    assert called.json()["result"]["isError"] is True
    execute.assert_not_awaited()


async def test_concurrent_stateless_calls_keep_identity_context_isolated(
    mcp_client: httpx.AsyncClient,
    active_user: UserRecord,
) -> None:
    actor = SearchActor(user=active_user, actor_type="access_key", access_key_id=uuid4())
    response = PublicSearchResponse(
        query="retrieval",
        strength=SearchStrength.STANDARD,
        degraded=False,
        hits=[],
        result_count=0,
        elapsed_ms=1.0,
    )
    invocations: list[SearchInvocation] = []

    async def execute(
        _request: PublicSearchRequest,
        invocation: SearchInvocation,
    ) -> PublicSearchResponse:
        invocations.append(invocation)
        await asyncio.sleep(0)
        return response

    call = _request(
        "tools/call",
        request_id=12,
        params={"name": "search_papers", "arguments": {"query": "retrieval"}},
    )
    with (
        patch(
            "scholight.api.mcp_server.resolve_access_key_search_actor",
            new_callable=AsyncMock,
            return_value=actor,
        ),
        patch("scholight.api.mcp_server.execute_public_search", side_effect=execute),
    ):
        anonymous_response, key_response = await asyncio.gather(
            mcp_client.post("/mcp", headers=_MCP_HEADERS, json=call),
            mcp_client.post(
                "/mcp",
                headers={
                    **_MCP_HEADERS,
                    "authorization": "Bearer sk_live_0123456789abcdef_secret",
                },
                json={**call, "id": 13},
            ),
        )

    assert (anonymous_response.status_code, key_response.status_code) == (200, 200)
    actors = [invocation.actor for invocation in invocations]
    assert any(value is None for value in actors)
    assert any(value is actor for value in actors)


async def test_rest_and_mcp_share_minute_bucket_but_handshake_does_not_consume_it(
    mcp_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_anonymous_minute_limits()
    monkeypatch.setattr(settings, "anonymous_rate_limit_per_minute", 1)
    reservation = AnonymousQuotaReservation(
        quota_date=datetime(2026, 7, 21, tzinfo=UTC).date(),
        ip_digest=b"d" * 32,
        strength="standard",
        used_count=1,
    )
    search_result = SearchResult(query="retrieval", level=1, total_ms=1.0, hits=[])

    try:
        with (
            patch(
                "scholight.api.search_access.reserve_anonymous_daily_quota",
                new_callable=AsyncMock,
                return_value=reservation,
            ),
            patch(
                "scholight.search.engine.SearchEngine.search",
                new_callable=AsyncMock,
                return_value=search_result,
            ),
        ):
            initialized = await _initialize(mcp_client, request_id=14)
            listed = await mcp_client.post(
                "/mcp",
                headers=_MCP_HEADERS,
                json=_request("tools/list", request_id=15, params={}),
            )
            searched = await mcp_client.post(
                "/mcp",
                headers=_MCP_HEADERS,
                json=_request(
                    "tools/call",
                    request_id=16,
                    params={"name": "search_papers", "arguments": {"query": "retrieval"}},
                ),
            )
            limited = await mcp_client.post("/search", json={"query": "retrieval"})
    finally:
        reset_anonymous_minute_limits()

    assert (initialized.status_code, listed.status_code, searched.status_code) == (200, 200, 200)
    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "anonymous_rate_limit_exceeded"
