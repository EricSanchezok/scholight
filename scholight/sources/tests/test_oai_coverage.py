"""Partial, malformed or looping OAI pages cannot prove complete revision coverage."""

from unittest.mock import AsyncMock

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
