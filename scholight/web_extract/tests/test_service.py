from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from scholight.web_extract.engine import ExtractDocument, ExtractInput
from scholight.web_extract.service import create_extract_service


class _Engine:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, request: ExtractInput) -> ExtractDocument:
        self.calls += 1
        return ExtractDocument(
            requested_url=request.url,
            final_url=request.url,
            status_code=200,
            title="Example",
            author=None,
            published_at=None,
            content_type="text/html",
            content="Extracted content",
            rendered=False,
            extractor="trafilatura",
            warnings=(),
            content_hash="a" * 64,
            fetched_at=datetime(2026, 8, 1, tzinfo=UTC),
        )


@pytest.mark.asyncio
async def test_extract_service_requires_internal_token() -> None:
    app = create_extract_service(engine=_Engine(), internal_token="internal-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://extract"
    ) as client:
        response = await client.post("/v1/extract", json={"url": "https://example.com"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_extract_service_returns_structured_document() -> None:
    app = create_extract_service(engine=_Engine(), internal_token="internal-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://extract"
    ) as client:
        response = await client.post(
            "/v1/extract",
            headers={"X-Scholight-Internal-Token": "internal-secret"},
            json={"url": "https://example.com"},
        )

    assert response.status_code == 200
    assert response.json()["content"] == "Extracted content"


@pytest.mark.asyncio
async def test_extract_service_caches_public_target_request() -> None:
    engine = _Engine()
    app = create_extract_service(engine=engine, internal_token="internal-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://extract"
    ) as client:
        for _ in range(2):
            response = await client.post(
                "/v1/extract",
                headers={"X-Scholight-Internal-Token": "internal-secret"},
                json={"url": "https://example.com"},
            )
            assert response.status_code == 200

    assert engine.calls == 1


@pytest.mark.asyncio
async def test_extract_service_does_not_share_cache_with_target_credentials() -> None:
    engine = _Engine()
    app = create_extract_service(engine=engine, internal_token="internal-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://extract"
    ) as client:
        for _ in range(2):
            response = await client.post(
                "/v1/extract",
                headers={"X-Scholight-Internal-Token": "internal-secret"},
                json={
                    "url": "https://example.com/private",
                    "headers": {"Authorization": "Bearer target"},
                },
            )
            assert response.status_code == 200

    assert engine.calls == 2
