"""App-side extract materialization contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from scholight.survey.extracts import (
    ExtractAttemptState,
    build_html_extract,
    build_pdf_extract,
    html_to_markdown,
    materialize_extracts,
    planned_paper_ids,
    run_extract_pass,
)

_HTML_FIXTURE = """<html><head><title>Paper</title><style>body{}</style></head>
<body>
<nav>skip me</nav>
<h1>Attention Is All You Need</h1>
<p>We minimize <math display="block" alttext="\\mathcal{L}(\\theta) = \\sum_i \\ell_i">L(theta)</math> over the corpus.</p>
<p>Inline <math alttext="\\alpha_i">alpha</math> weights are learned.</p>
<math>no alttext math</math>
<table>
  <tr><th>Method</th><th>Bleu</th></tr>
  <tr><td>Base | v2</td><td>27.3</td></tr>
  <tr><td>Ours</td><td>28.4</td></tr>
</table>
<figure><img src="fig1.png"/><figcaption>Overview of the architecture.</figcaption></figure>
</body></html>"""


def _plan(root: Path, paper_ids: list[str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "00_card_plan.json").write_text(
        json.dumps([{"run_dir": ".", "id": pid, "title": "T", "why": "w"} for pid in paper_ids]),
        encoding="utf-8",
    )


def test_html_conversion_preserves_alttext_latex_and_tables() -> None:
    markdown = html_to_markdown(_HTML_FIXTURE)

    assert "$$\\mathcal{L}(\\theta) = \\sum_i \\ell_i$$" in markdown
    assert "$\\alpha_i$" in markdown
    assert "skip me" not in markdown
    assert "| Method | Bleu |" in markdown
    assert "| --- | --- |" in markdown
    assert "| Base \\| v2 | 27.3 |" in markdown
    assert "| Ours | 28.4 |" in markdown
    assert "fig1.png" not in markdown
    assert "Overview of the architecture." in markdown
    assert "no alttext math" not in markdown


def test_build_html_extract_declares_html_evidence() -> None:
    document = build_html_extract(_HTML_FIXTURE, source="https://arxiv.org/html/2401.12345")

    assert document.startswith("<!-- app-materialized extract: do not edit -->")
    assert "## evidence" in document
    assert "- level: html" in document
    assert "- reason: html_text_extracted" in document
    assert "- source: https://arxiv.org/html/2401.12345" in document
    assert "$$\\mathcal{L}(\\theta) = \\sum_i \\ell_i$$" in document


def test_build_html_extract_truncates_to_partial() -> None:
    huge = "<html><body>" + ("<p>word</p>" * 200_000) + "</body></html>"

    document = build_html_extract(huge, source="https://arxiv.org/html/2401.1")

    assert "- level: partial" in document
    assert "- reason: html_text_truncated" in document
    assert len(document) < 500_000


def test_build_pdf_extract_declares_markdown_evidence() -> None:
    document = build_pdf_extract("## Introduction\n\nBody text.", stem="2401.12345")

    assert "- level: full_text" in document
    assert "- reason: pdf_markdown_extracted" in document
    assert "- source: pdfs/2401.12345.pdf" in document
    assert "## Introduction" in document


def test_planned_paper_ids_reads_durable_plan(tmp_path: Path) -> None:
    _plan(tmp_path, ["2401.12345", "math/0208020"])

    assert planned_paper_ids(tmp_path) == ("2401.12345", "math/0208020")


def test_planned_paper_ids_tolerates_missing_or_broken_plan(tmp_path: Path) -> None:
    assert planned_paper_ids(tmp_path) == ()

    (tmp_path / "00_card_plan.json").write_text("not json", encoding="utf-8")
    assert planned_paper_ids(tmp_path) == ()

    (tmp_path / "00_card_plan.json").write_text('{"not":"a list"}', encoding="utf-8")
    assert planned_paper_ids(tmp_path) == ()


@pytest.mark.asyncio
async def test_pass_materializes_html_from_plan(tmp_path: Path) -> None:
    _plan(tmp_path, ["2401.12345"])
    events: list[tuple[str, dict[str, object]]] = []

    def transport(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "arxiv.org"
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=_HTML_FIXTURE,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        follow_redirects=True,
    ) as client:
        await run_extract_pass(
            tmp_path,
            state=ExtractAttemptState(),
            client=client,
            on_event=lambda kind, fields: events.append((kind, fields)),
        )

    extract = tmp_path / "extracts" / "2401.12345.md"
    assert extract.is_file()
    assert "- level: html" in extract.read_text(encoding="utf-8")
    assert [kind for kind, _ in events] == ["extract_written"]
    assert events[0][1]["source"] == "html"


@pytest.mark.asyncio
async def test_pass_falls_back_to_ar5iv(tmp_path: Path) -> None:
    _plan(tmp_path, ["1909.08053"])

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.host == "arxiv.org":
            return httpx.Response(404)
        assert request.url.host == "ar5iv.labs.arxiv.org"
        return httpx.Response(200, headers={"content-type": "text/html"}, text=_HTML_FIXTURE)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        follow_redirects=True,
    ) as client:
        await run_extract_pass(tmp_path, state=ExtractAttemptState(), client=client)

    assert (tmp_path / "extracts" / "1909.08053.md").is_file()


@pytest.mark.asyncio
async def test_pass_skips_stems_with_existing_or_attempted_extracts(tmp_path: Path) -> None:
    _plan(tmp_path, ["2401.12345", "2402.22222"])
    (tmp_path / "extracts").mkdir()
    (tmp_path / "extracts" / "2401.12345.md").write_text("existing", encoding="utf-8")

    calls: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.host)
        return httpx.Response(500)

    state = ExtractAttemptState()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport),
        follow_redirects=True,
    ) as client:
        await run_extract_pass(tmp_path, state=state, client=client)
        assert calls == ["arxiv.org", "ar5iv.labs.arxiv.org"]  # only 2402.22222

        calls.clear()
        await run_extract_pass(tmp_path, state=state, client=client)
        assert calls == []  # attempted stems are never retried


@pytest.mark.asyncio
async def test_pass_materializes_new_pdfs_without_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    (pdfs / "2401.12345.pdf").write_bytes(b"%PDF-1.4 stub")

    def fake_pdf_to_markdown(path: str) -> str:
        assert path.endswith("2401.12345.pdf")
        return "## Body\n\nconverted"

    monkeypatch.setattr(
        "scholight.pipeline.pdf_md.pdf_to_markdown",
        fake_pdf_to_markdown,
    )

    events: list[tuple[str, dict[str, object]]] = []
    state = ExtractAttemptState()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as client:
        await run_extract_pass(
            tmp_path,
            state=state,
            client=client,
            on_event=lambda kind, fields: events.append((kind, fields)),
        )
        await run_extract_pass(tmp_path, state=state, client=client)

    extract = tmp_path / "extracts" / "2401.12345.md"
    assert "- reason: pdf_markdown_extracted" in extract.read_text(encoding="utf-8")
    assert [kind for kind, _ in events] == ["extract_written"]


@pytest.mark.asyncio
async def test_pass_isolates_pdf_conversion_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdfs = tmp_path / "pdfs"
    pdfs.mkdir()
    (pdfs / "bad.pdf").write_bytes(b"%PDF-1.4 stub")

    def boom(path: str) -> str:
        raise RuntimeError("scanned pdf")

    monkeypatch.setattr("scholight.pipeline.pdf_md.pdf_to_markdown", boom)

    events: list[tuple[str, dict[str, object]]] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as c:
        await run_extract_pass(
            tmp_path,
            state=ExtractAttemptState(),
            client=c,
            on_event=lambda kind, fields: events.append((kind, fields)),
        )

    assert [kind for kind, _ in events] == ["extract_failed"]
    assert not (tmp_path / "extracts" / "bad.md").exists()


@pytest.mark.asyncio
async def test_materializer_loop_stops_on_event(tmp_path: Path) -> None:
    stop = asyncio.Event()
    started = asyncio.Event()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
        follow_redirects=True,
    ) as client:
        task = asyncio.create_task(
            materialize_extracts(tmp_path, stop=stop, client=client, poll_interval=0.05)
        )
        _plan(tmp_path, [])
        await asyncio.sleep(0.15)
        started.set()
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    assert task.exception() is None
