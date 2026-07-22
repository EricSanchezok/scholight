"""Public search-history API and background-write tests."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cloud_auth.models.user import UserRecord
from fastapi import FastAPI

from scholight.api.deps import SearchActor, get_current_user, get_optional_search_actor
from scholight.api.search_access import SearchQuotaReservation
from scholight.db.client import DBError
from scholight.models.history import SearchHistoryEntry
from scholight.models.search import SearchResult


def _history_entry(entry_id: int = 9, *, level: int = 2) -> SearchHistoryEntry:
    return SearchHistoryEntry(
        id=entry_id,
        query_text="retrieval",
        level=level,
        strategy="internal-only",
        filters={"categories": ["cs.AI"]},
        num_results=3,
        response_time_ms=12.5,
        created_at=datetime(2026, 7, 21, 10, 15, 30, 123000, tzinfo=UTC),
    )


def _authenticate(api_app: FastAPI, user: object) -> None:
    api_app.dependency_overrides[get_current_user] = lambda: user


@pytest.mark.asyncio
async def test_history_requires_authentication(api_client: httpx.AsyncClient) -> None:
    response = await api_client.get("/search/history")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_history_returns_public_page_and_normalizes_query(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
) -> None:
    _authenticate(api_app, active_user)
    page = SimpleNamespace(items=[_history_entry()], total=1, legacy_level3_count=0)

    with patch(
        "scholight.api.routes.search.get_search_history",
        new_callable=AsyncMock,
        return_value=page,
    ) as get_history:
        response = await api_client.get(
            "/search/history", params={"limit": 10, "offset": 5, "q": "  retrieval  "}
        )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": 9,
                "query": "retrieval",
                "strength": "thorough",
                "filters": {
                    "categories": ["cs.AI"],
                    "authors": [],
                    "date_from": None,
                    "date_to": None,
                },
                "result_count": 3,
                "elapsed_ms": 12.5,
                "created_at": "2026-07-21T10:15:30.123000Z",
            }
        ],
        "total": 1,
        "limit": 10,
        "offset": 5,
    }
    get_history.assert_awaited_once_with(42, limit=10, offset=5, q="retrieval")


@pytest.mark.asyncio
async def test_history_sanitizes_malformed_legacy_filters_without_failing_page(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
) -> None:
    _authenticate(api_app, active_user)
    entry = _history_entry()
    entry.filters = {
        "categories": ["cs.AI", "", "invalid!", 7],
        "authors": ["Ada Lovelace", "x" * 201, None],
        "date_from": "not-a-date",
        "date_to": "2024-12-31",
    }
    page = SimpleNamespace(items=[entry], total=1, legacy_level3_count=0)

    with patch(
        "scholight.api.routes.search.get_search_history",
        new_callable=AsyncMock,
        return_value=page,
    ):
        response = await api_client.get("/search/history")

    assert response.status_code == 200
    assert response.json()["items"][0]["filters"] == {
        "categories": ["cs.AI"],
        "authors": ["Ada Lovelace"],
        "date_from": None,
        "date_to": "2024-12-31",
    }


@pytest.mark.asyncio
async def test_history_empty_q_is_normalized_to_none(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
) -> None:
    _authenticate(api_app, active_user)
    page = SimpleNamespace(items=[], total=0, legacy_level3_count=0)

    with patch(
        "scholight.api.routes.search.get_search_history",
        new_callable=AsyncMock,
        return_value=page,
    ) as get_history:
        response = await api_client.get("/search/history", params={"q": "   "})

    assert response.status_code == 200
    get_history.assert_awaited_once_with(42, limit=20, offset=0, q=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"q": "x" * 201},
    ],
)
async def test_history_query_validation_returns_422(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    params: dict[str, str | int],
) -> None:
    _authenticate(api_app, active_user)

    response = await api_client.get("/search/history", params=params)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_bulk_delete_stably_deduplicates_and_returns_count(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
) -> None:
    _authenticate(api_app, active_user)

    with patch(
        "scholight.api.routes.search.bulk_soft_delete_search_entries",
        new_callable=AsyncMock,
        return_value=2,
        create=True,
    ) as bulk_delete:
        response = await api_client.post("/search/history/bulk-delete", json={"ids": [3, 1, 3, 99]})

    assert (response.status_code, response.json()) == (200, {"deleted": 2})
    bulk_delete.assert_awaited_once_with(42, [3, 1, 99])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"ids": []},
        {"ids": [0]},
        {"ids": [-1]},
        {"ids": [True]},
        {"ids": [1.0]},
        {"ids": ["1"]},
        {"ids": list(range(1, 102))},
        {"ids": [2**63]},
        {"ids": [1], "extra": True},
    ],
)
async def test_bulk_delete_request_is_strict(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
    body: dict[str, object],
) -> None:
    _authenticate(api_app, active_user)

    response = await api_client.post("/search/history/bulk-delete", json=body)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_single_delete_success_and_404_remain_compatible(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
) -> None:
    _authenticate(api_app, active_user)

    with patch(
        "scholight.api.routes.search.soft_delete_search_entry",
        new_callable=AsyncMock,
        side_effect=[True, False],
    ):
        deleted = await api_client.delete("/search/history/7")
        missing = await api_client.delete("/search/history/8")

    assert (deleted.status_code, deleted.json()) == (200, {"message": "Deleted"})
    assert (missing.status_code, missing.json()) == (404, {"detail": "Entry not found"})


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["get", "post", "delete"])
async def test_history_database_errors_return_stable_503(
    api_app: FastAPI,
    active_user: UserRecord,
    method: str,
) -> None:
    _authenticate(api_app, active_user)
    transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
    with (
        patch(
            "scholight.api.routes.search.get_search_history",
            new_callable=AsyncMock,
            side_effect=DBError("private SQL detail"),
        ),
        patch(
            "scholight.api.routes.search.bulk_soft_delete_search_entries",
            new_callable=AsyncMock,
            side_effect=DBError("private SQL detail"),
            create=True,
        ),
        patch(
            "scholight.api.routes.search.soft_delete_search_entry",
            new_callable=AsyncMock,
            side_effect=DBError("private SQL detail"),
        ),
    ):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            if method == "get":
                response = await client.get("/search/history")
            elif method == "post":
                response = await client.post("/search/history/bulk-delete", json={"ids": [1]})
            else:
                response = await client.delete("/search/history/1")

    assert (response.status_code, response.headers["retry-after"], response.json()) == (
        503,
        "5",
        {
            "detail": {
                "code": "history_unavailable",
                "message": "Search history is temporarily unavailable.",
                "retryable": True,
            }
        },
    )


@pytest.mark.asyncio
async def test_authenticated_final_200_schedules_normalized_history(
    api_app: FastAPI,
    api_client: httpx.AsyncClient,
    active_user: UserRecord,
) -> None:
    api_app.dependency_overrides[get_optional_search_actor] = lambda: SearchActor(
        user=active_user, actor_type="web"
    )
    reservation = SearchQuotaReservation(operation="search_level1")
    result = SearchResult(query="retrieval", level=1, total_ms=1.0, hits=[])

    with (
        patch(
            "scholight.api.routes.search.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ),
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            return_value=result,
        ),
        patch(
            "scholight.api.routes.search.schedule_search_history_write",
            create=True,
        ) as schedule,
    ):
        response = await api_client.post(
            "/search",
            json={
                "query": "  retrieval  ",
                "filters": {"categories": ["cs.AI"]},
            },
        )

    assert response.status_code == 200
    schedule.assert_called_once()
    assert schedule.call_args.kwargs["user_id"] == 42
    assert schedule.call_args.kwargs["query_text"] == "retrieval"
    assert schedule.call_args.kwargs["level"] == 1
    assert schedule.call_args.kwargs["filters"] == {"categories": ["cs.AI"]}
    assert schedule.call_args.kwargs["result_count"] == 0
    assert schedule.call_args.kwargs["strength"] == "standard"
    assert schedule.call_args.kwargs["request_id"] == response.headers["x-request-id"]


@pytest.mark.asyncio
async def test_history_write_failure_is_consumed_and_structured() -> None:
    history_tasks = importlib.import_module("scholight.api.history_tasks")

    with (
        patch.object(
            history_tasks,
            "log_search",
            new_callable=AsyncMock,
            side_effect=DBError("private SQL detail"),
        ),
        patch.object(history_tasks.logger, "warning") as warning,
    ):
        history_tasks.schedule_search_history_write(
            request_id="request-123",
            user_id=42,
            query_text="private query",
            level=2,
            strength="thorough",
            filters=None,
            result_count=3,
            elapsed_ms=12.5,
        )
        await history_tasks.drain_search_history_tasks()

    kwargs = warning.call_args.kwargs
    assert warning.call_args.args == ("search_history_write_failed",)
    assert kwargs["request_id"] == "request-123"
    assert kwargs["user_id"] == 42
    assert kwargs["strength"] == "thorough"
    assert kwargs["result_count"] == 3
    assert kwargs["error_type"] == "DBError"
    assert kwargs["retryable"] is True
    assert kwargs["duration_ms"] >= 0
    assert "query" not in kwargs


@pytest.mark.asyncio
async def test_lifespan_drains_history_tasks_before_closing_pool() -> None:
    from scholight.api.app import lifespan

    order: list[str] = []

    async def drain() -> None:
        order.append("drain")

    async def close() -> None:
        order.append("close")

    with (
        patch("scholight.db.client.create_pool", new_callable=AsyncMock),
        patch("scholight.api.history_tasks.drain_search_history_tasks", new=drain),
        patch("scholight.db.client.close_pool", new=close),
        patch("scholight.store.client.get_client"),
    ):
        async with lifespan(MagicMock()):
            pass

    assert order == ["drain", "close"]
