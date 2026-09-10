"""Run inside native images: process lifecycle with isolated external boundaries."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


def metadata_smoke() -> None:
    from scholight.config import settings
    from scholight.scheduler import metadata_sync
    from scholight.store.ingestion import MetadataOutcome

    assert settings.runtime_profile == "lean"
    assert importlib.util.find_spec("fitz") is None
    assert importlib.util.find_spec("weasyprint") is None
    assert importlib.util.find_spec("matplotlib") is None
    import datetime as dt

    with (
        patch.object(metadata_sync, "_fetch_day", AsyncMock(return_value=([{}], "oai"))),
        patch.object(metadata_sync, "_normalize_and_embed", AsyncMock()),
        patch.object(
            metadata_sync,
            "write_metadata_papers",
            return_value=[MetadataOutcome("2401.00001", 2, "revision")],
        ),
        patch.object(metadata_sync, "record_deferred_fulltext", AsyncMock()) as ledger,
        patch.object(
            metadata_sync, "enqueue_ingestion_job", side_effect=AssertionError("fulltext enqueue")
        ),
        patch.object(
            metadata_sync, "list_missing_chunks", side_effect=AssertionError("chunks read")
        ),
    ):
        asyncio.run(metadata_sync._sync_day(dt.date(2026, 9, 10), dt.date(2026, 9, 10)))
        assert ledger.await_count == 1


def api_smoke() -> None:
    from fastapi.testclient import TestClient

    from scholight.api.app import create_app
    from scholight.api.deps import get_current_user
    from scholight.config import settings
    from scholight.models.search import SearchResult

    settings.auth_jwt_secret = settings.jwt_secret = "j" * 32
    settings.anonymous_quota_hmac_secret = "h" * 32
    settings.access_key_hmac_secret = "k" * 32
    settings.mcp_delegation_jwt_secret = "d" * 32
    settings.zilliz_uri = "http://127.0.0.1:9"
    settings.zilliz_token = "isolated-test-token"
    settings.embedding_base_url = "http://127.0.0.1:9/v1"
    settings.extract_internal_token = "x" * 32
    settings.cors_allow_origins = ["http://localhost:7200"]
    assert settings.runtime_profile == "lean"
    assert importlib.util.find_spec("weasyprint") is None
    assert importlib.util.find_spec("matplotlib") is None
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=42, status="active")
    engine = SimpleNamespace(
        search=AsyncMock(
            return_value=SearchResult(query="retrieval", level=1, total_ms=1.0, hits=[])
        )
    )
    with (
        patch("scholight.db.client.create_pool", AsyncMock()),
        patch("scholight.db.client.close_pool", AsyncMock()),
        patch(
            "scholight.store.client.get_client",
            return_value=SimpleNamespace(list_collections=lambda **_: ["arxiv_papers"]),
        ),
        patch("scholight.api.search_execution.reserve_search_quota", AsyncMock()) as quota,
        patch("scholight.search.engine.SearchEngine", return_value=engine),
        TestClient(app) as client,
    ):
        assert client.get("/livez").status_code == 200
        assert (
            client.post("/search", json={"query": "retrieval", "strength": "thorough"}).status_code
            == 422
        )
        assert quota.await_count == 0
        response = client.get("/capabilities")
        assert response.status_code == 200 and response.json()["survey"] == "off"
        for body in ({"query": "retrieval"}, {"query": "retrieval", "strength": "standard"}):
            result = client.post("/search", json=body)
            assert result.status_code == 200, result.text
            assert result.json()["strength"] == "standard"
        assert client.get("/surveys").status_code == 404


if __name__ == "__main__":
    {"api": api_smoke, "metadata": metadata_smoke}[sys.argv[1]]()
