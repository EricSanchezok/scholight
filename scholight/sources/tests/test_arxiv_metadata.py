"""Metadata semantics and API fallback request contracts."""

from __future__ import annotations

import datetime as dt
from typing import Any
from unittest.mock import patch

import pytest

from scholight.sources.arxiv import _parse_record, fetch_papers_api, fetch_papers_by_ids


def test_oai_version_dates_define_created_and_updated() -> None:
    paper = _parse_record(
        """
        <record>
          <header>
            <identifier>oai:arXiv.org:2401.00001</identifier>
            <datestamp>2026-07-24</datestamp>
          </header>
          <metadata>
            <title>Versioned paper</title>
            <versions>
              <version><date>2 Jan 2024</date></version>
              <version><date>3 Feb 2024</date></version>
            </versions>
          </metadata>
        </record>
        """
    )

    assert paper is not None
    assert (paper["created"], paper["updated"]) == ("2024-01-02", "2024-02-03")


def test_oai_datestamp_is_fallback_when_version_dates_are_absent() -> None:
    paper = _parse_record(
        """
        <record>
          <header>
            <identifier>oai:arXiv.org:2401.00001</identifier>
            <datestamp>2026-07-24</datestamp>
          </header>
          <metadata><title>Unversioned paper</title></metadata>
        </record>
        """
    )

    assert paper is not None
    assert (paper["created"], paper["updated"]) == ("2026-07-24", "2026-07-24")


def test_oai_authors_are_split_from_the_current_comma_separated_format() -> None:
    paper = _parse_record(
        """
        <record>
          <header>
            <identifier>oai:arXiv.org:2604.02334</identifier>
            <datestamp>2026-04-06</datestamp>
          </header>
          <metadata>
            <authors>Xiaohang Nie, Zihan Guo, and Weinan Zhang</authors>
          </metadata>
        </record>
        """
    )

    assert paper is not None
    assert paper["authors"] == ["Xiaohang Nie", "Zihan Guo", "Weinan Zhang"]


@pytest.mark.asyncio
async def test_api_fallback_uses_spaces_in_submitted_date_query() -> None:
    captured: dict[str, Any] = {}

    class Response:
        status_code = 200
        text = ""

        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _url: str, *, params: dict[str, Any]) -> Response:
            captured.update(params)
            return Response()

    with patch("scholight.sources.arxiv.httpx.AsyncClient", return_value=Client()):
        papers = await fetch_papers_api(dt.date(2026, 7, 18))

    assert papers == []
    assert captured["search_query"] == "submittedDate:[202607180000 TO 202607182359]"


@pytest.mark.asyncio
async def test_api_batch_fetches_metadata_by_arxiv_id() -> None:
    captured: dict[str, Any] = {}
    client_options: dict[str, Any] = {}

    class Response:
        status_code = 200
        text = """
        <feed>
          <entry>
            <id>http://arxiv.org/abs/2604.02334v1</id>
            <title>Holos</title>
            <published>2026-01-18T13:09:25Z</published>
            <updated>2026-01-18T13:09:25Z</updated>
            <author><name>Xiaohang Nie</name></author>
            <author><name>Zihan Guo</name></author>
          </entry>
        </feed>
        """

        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, _url: str, *, params: dict[str, Any]) -> Response:
            captured.update(params)
            return Response()

    def client_factory(**kwargs: Any) -> Client:
        client_options.update(kwargs)
        return Client()

    with patch("scholight.sources.arxiv.httpx.AsyncClient", side_effect=client_factory):
        papers = await fetch_papers_by_ids(["2604.02334"], timeout_seconds=90)

    assert client_options["timeout"] == 90
    assert captured == {"id_list": "2604.02334", "max_results": 1}
    assert papers[0]["authors"] == ["Xiaohang Nie", "Zihan Guo"]
