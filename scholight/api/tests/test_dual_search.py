"""Public modes share transport-neutral accounting and fail closed before reservation."""

from unittest.mock import AsyncMock, patch

import pytest
from pymilvus.exceptions import MilvusException
from sanchezcloud_identity.models.user import UserRecord

from scholight.api.deps import SearchActor
from scholight.api.models.search import PublicSearchRequest, SearchStrength
from scholight.api.search_access import SearchQuotaReservation
from scholight.api.search_execution import (
    PublicSearchError,
    SearchInvocation,
    execute_public_search,
)
from scholight.config import settings
from scholight.models.search import SearchResult
from scholight.search.errors import ThoroughSearchUnavailable


@pytest.mark.parametrize("strength,level", [("standard", 1), ("thorough", 2)])
def test_public_modes_select_original_algorithm(strength: str, level: int) -> None:
    request = PublicSearchRequest.model_validate({"query": "retrieval", "strength": strength})
    assert request.to_internal().level == level


@pytest.mark.asyncio
@pytest.mark.parametrize("actor_type", ["web", "access_key", "delegated", None])
async def test_thorough_accounts_for_actual_mode(
    monkeypatch: pytest.MonkeyPatch, active_user: UserRecord, actor_type: str | None
) -> None:
    monkeypatch.setattr(settings, "runtime_profile", "full")
    monkeypatch.setattr(settings, "public_thorough_enabled", True)
    actor = SearchActor(user=active_user, actor_type=actor_type) if actor_type else None  # type: ignore[arg-type]
    invocation = SearchInvocation(
        actor=actor, client_ip="192.0.2.1", request_id="dual", transport="rest"
    )
    with (
        patch(
            "scholight.api.search_execution.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=SearchQuotaReservation(strength="thorough"),
        ) as reserve,
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            return_value=SearchResult(query="retrieval", level=2, total_ms=1, hits=[]),
        ) as search,
        patch("scholight.api.search_execution.schedule_search_history_write") as history,
        patch("scholight.api.search_execution.schedule_usage_event") as usage,
    ):
        response = await execute_public_search(
            PublicSearchRequest.model_validate({"query": "retrieval", "strength": "thorough"}),
            invocation,
        )
    assert response.strength == "thorough"
    assert reserve.call_args.kwargs["strength"] == "thorough"
    assert search.call_args.args[0].level == 2
    if actor:
        assert history.call_args.kwargs["strength"] == "thorough"
        assert usage.call_args.args[0].strength == "thorough"


@pytest.mark.asyncio
async def test_thorough_dependency_failure_refunds_without_standard_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "runtime_profile", "full")
    monkeypatch.setattr(settings, "public_thorough_enabled", True)
    reservation = SearchQuotaReservation(strength="thorough")
    with (
        patch(
            "scholight.api.search_execution.reserve_search_quota",
            new_callable=AsyncMock,
            return_value=reservation,
        ),
        patch(
            "scholight.api.search_execution.compensate_search_quota", new_callable=AsyncMock
        ) as refund,
        patch(
            "scholight.search.engine.SearchEngine.search",
            new_callable=AsyncMock,
            side_effect=ThoroughSearchUnavailable(
                phase_name="chunk_search", cause=MilvusException(message="offline")
            ),
        ) as search,
        pytest.raises(PublicSearchError, match="Thorough search") as error,
    ):
        await execute_public_search(
            PublicSearchRequest.model_validate({"query": "retrieval", "strength": "thorough"}),
            SearchInvocation(actor=None, client_ip="192.0.2.1", request_id="fail", transport="mcp"),
        )
    assert error.value.code == "thorough_search_unavailable"
    refund.assert_awaited_once_with(reservation)
    search.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,enabled", [("lean", False), ("lean", True), ("full", False)])
async def test_unavailable_mode_does_not_reserve(
    profile: str, enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "runtime_profile", profile)
    monkeypatch.setattr(settings, "public_thorough_enabled", enabled)
    with patch(
        "scholight.api.search_execution.reserve_search_quota", new_callable=AsyncMock
    ) as reserve:
        with pytest.raises(PublicSearchError) as error:
            await execute_public_search(
                PublicSearchRequest(query="retrieval", strength=SearchStrength.THOROUGH),
                SearchInvocation(
                    actor=None, client_ip="192.0.2.1", request_id="disabled", transport="rest"
                ),
            )
    assert error.value.status_code == 422
    reserve.assert_not_called()
