"""Partial, malformed or looping OAI pages cannot prove complete revision coverage."""

from inspect import unwrap
from unittest.mock import AsyncMock

import httpx
import pytest

from scholight.sources import arxiv


@pytest.mark.asyncio
async def test_repeated_resumption_token_is_not_success(monkeypatch: pytest.MonkeyPatch) -> None:
    page = '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords><resumptionToken>repeat</resumptionToken></ListRecords></OAI-PMH>'
    monkeypatch.setattr(arxiv, "_fetch_oai_page", AsyncMock(return_value=page))
    monkeypatch.setattr("scholight.sources.arxiv.asyncio.sleep", AsyncMock())
    with pytest.raises(arxiv.OAIHarvestError, match="token"):
        await arxiv.iter_papers_oai("2026-09-13", "2026-09-13")


@pytest.mark.asyncio
async def test_malformed_response_is_not_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        arxiv, "_fetch_oai_page", AsyncMock(return_value="<html>maintenance</html>")
    )
    with pytest.raises(arxiv.OAIHarvestError, match="OAI"):
        await arxiv.iter_papers_oai("2026-09-13", "2026-09-13")


@pytest.mark.asyncio
async def test_unparsable_active_record_prevents_coverage_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><ListRecords><record><header><identifier>oai:arXiv.org:2609.00001</identifier></header><metadata>bad</metadata></record></ListRecords></OAI-PMH>'
    monkeypatch.setattr(arxiv, "_fetch_oai_page", AsyncMock(return_value=page))
    with pytest.raises(arxiv.OAIHarvestError, match="record"):
        await arxiv.iter_papers_oai("2026-09-13", "2026-09-13")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        '<html><error code="noRecordsMatch">maintenance</error></html>',
        '<OAI-PMH xmlns="http://www.openarchives.org/OAI/2.0/"><error code="noRecordsMatch">none</error>',
    ],
)
async def test_empty_day_error_must_be_complete_oai_xml(
    monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    response = httpx.Response(200, text=body, request=httpx.Request("GET", "https://example.org"))
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = response
    monkeypatch.setattr("scholight.sources.arxiv.httpx.AsyncClient", lambda **kwargs: client)
    with pytest.raises(arxiv.OAIHarvestError, match="OAI"):
        await unwrap(arxiv._fetch_oai_page)("https://example.org")
