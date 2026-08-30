"""App-side extract materialization contracts for Survey workspaces."""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString
from markdownify import MarkdownConverter
from tenacity import retry, retry_if_exception, stop_after_attempt

from scholight.sources.arxiv import arxiv_artifact_stem
from scholight.utils.http import is_transient

_EXTRACT_MARKER = "<!-- app-materialized extract: do not edit -->"
_ARXIV_HTML_URL = "https://arxiv.org/html/{paper_id}"
_AR5IV_HTML_URL = "https://ar5iv.labs.arxiv.org/html/{paper_id}"
_EXTRACT_MAX_CHARS = 400_000
_FETCH_TIMEOUT_SECONDS = 30.0
_MAX_CONCURRENT_EXTRACTS = 2
_DEFAULT_POLL_INTERVAL_SECONDS = 3.0

_EventCallback = Callable[[str, dict[str, object]], None]


# ── Pure HTML → Markdown conversion ──────────────────────────────────────────


def _gfm_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def _table_to_gfm(table: Tag) -> str:
    rows = table.find_all("tr")
    rendered: list[list[str]] = []
    for row in rows:
        header_cells = row.find_all(["th", "td"])
        if not header_cells:
            continue
        rendered.append([_gfm_cell(cell.get_text(" ", strip=True)) for cell in header_cells])
    if not rendered:
        return ""
    width = max(len(cells) for cells in rendered)
    padded = [cells + [""] * (width - len(cells)) for cells in rendered]
    lines = ["| " + " | ".join(padded[0]) + " |"]
    lines.append("| " + " | ".join("---" for _ in range(width)) + " |")
    for cells in padded[1:]:
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def html_to_markdown(html: str) -> str:
    """Convert an arXiv HTML rendering to evidence-grade markdown.

    Formulas keep their original LaTeX through ``<math alttext>``, tables become
    GFM pipe tables, and images are dropped so extracts stay self-contained.
    Formulas are swapped for plain tokens around the markdown conversion so the
    escaper cannot mangle ``$`` or backslashes inside LaTeX.
    """
    soup = BeautifulSoup(html, "html.parser")
    for element in soup.find_all(["script", "style", "nav"]):
        element.decompose()
    math_snippets: list[str] = []
    for math_tag in soup.find_all("math"):
        alttext = math_tag.get("alttext")
        if not isinstance(alttext, str) or not alttext.strip():
            math_tag.decompose()
            continue
        delimiter = "$$" if math_tag.get("display") == "block" else "$"
        math_snippets.append(f"{delimiter}{alttext.strip()}{delimiter}")
        math_tag.replace_with(NavigableString(f"\nMATHSNIPPET{len(math_snippets) - 1}ENDMATH\n"))
    for table in list(soup.find_all("table")):
        if table.find_parent("table") is not None:
            continue
        gfm = _table_to_gfm(table)
        table.replace_with(NavigableString(f"\n\n{gfm}\n\n" if gfm else ""))
    for image in soup.find_all("img"):
        image.decompose()
    converter = MarkdownConverter(heading_style="ATX")
    markdown = converter.convert_soup(soup)
    # Break the tree's reference cycles so the parsed DOM is freed immediately
    # instead of waiting for the next GC cycle under memory pressure.
    soup.decompose()
    for index, snippet in enumerate(math_snippets):
        markdown = markdown.replace(f"MATHSNIPPET{index}ENDMATH", snippet)
    return re.sub(r"\n{3,}", "\n\n", markdown).strip()


# ── Extract document assembly ────────────────────────────────────────────────


def _compose_document(
    body: str,
    *,
    level: str,
    reason: str,
    source: str,
) -> str:
    header = (
        f"{_EXTRACT_MARKER}\n"
        f"## evidence\n"
        f"- level: {level}\n"
        f"- reason: {reason}\n"
        f"- source: {source}\n"
    )
    return f"{header}\n{body.strip()}\n"


def build_html_extract(html: str, *, source: str) -> str:
    """Build the extract document from an arXiv/ar5iv HTML rendering."""
    body = html_to_markdown(html)
    level, reason = "html", "html_text_extracted"
    if len(body) > _EXTRACT_MAX_CHARS:
        body = body[:_EXTRACT_MAX_CHARS]
        level, reason = "partial", "html_text_truncated"
    return _compose_document(body, level=level, reason=reason, source=source)


def build_pdf_extract(body: str, *, stem: str) -> str:
    """Build the extract document from pymupdf4llm markdown."""
    level, reason = "full_text", "pdf_markdown_extracted"
    if len(body) > _EXTRACT_MAX_CHARS:
        body = body[:_EXTRACT_MAX_CHARS]
        level, reason = "partial", "pdf_markdown_truncated"
    return _compose_document(body, level=level, reason=reason, source=f"pdfs/{stem}.pdf")


def _write_extract(path: Path, document: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(document)
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def planned_paper_ids(run_dir: Path) -> tuple[str, ...]:
    """Return the canonical paper ids declared by the durable card plan."""
    plan_path = run_dir / "00_card_plan.json"
    try:
        payload: Any = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ()
    if not isinstance(payload, list):
        return ()
    ids = [
        item["id"] for item in payload if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    return tuple(ids)


# ── Async materialization ────────────────────────────────────────────────────


@dataclass(slots=True)
class ExtractAttemptState:
    """Tracks which stems this materializer already attempted, per source."""

    html_stems: set[str] = field(default_factory=set)
    pdf_stems: set[str] = field(default_factory=set)
    plan_consumed: bool = False


def _is_transient_http(exception: BaseException) -> bool:
    return is_transient(exception)


_EXTRACT_SEMAPHORES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)


def _extract_semaphore() -> asyncio.Semaphore:
    """Return the worker-wide extract semaphore bound to the running loop.

    All Survey jobs materializing in this worker process share one cap, so
    concurrent jobs cannot multiply peak conversion memory.  The per-loop
    cache keeps test sessions (one loop per test) isolated from each other.
    """
    loop = asyncio.get_running_loop()
    semaphore = _EXTRACT_SEMAPHORES.get(loop)
    if semaphore is None:
        semaphore = asyncio.Semaphore(_MAX_CONCURRENT_EXTRACTS)
        _EXTRACT_SEMAPHORES[loop] = semaphore
    return semaphore


@retry(reraise=True, stop=stop_after_attempt(2), retry=retry_if_exception(_is_transient_http))
async def _get_html(client: httpx.AsyncClient, url: str) -> httpx.Response:
    response = await client.get(url)
    response.raise_for_status()
    return response


async def _fetch_first_html(
    client: httpx.AsyncClient,
    paper_id: str,
) -> tuple[str, str] | None:
    for template in (_ARXIV_HTML_URL, _AR5IV_HTML_URL):
        url = template.format(paper_id=paper_id)
        try:
            response = await _get_html(client, url)
        except httpx.HTTPError:
            continue
        if response.status_code != 200:
            continue
        if "html" not in response.headers.get("content-type", "").lower():
            continue
        return response.text, url
    return None


async def _materialize_html(
    run_dir: Path,
    paper_id: str,
    stem: str,
    *,
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    on_event: _EventCallback | None,
) -> None:
    async with semaphore:
        fetched = await _fetch_first_html(client, paper_id)
    if fetched is None:
        if on_event is not None:
            on_event("extract_failed", {"paper_id": paper_id, "stem": stem, "source": "html"})
        return
    html, source = fetched
    document = build_html_extract(html, source=source)
    _write_extract(run_dir / "extracts" / f"{stem}.md", document)
    if on_event is not None:
        on_event(
            "extract_written",
            {"stem": stem, "source": "html", "chars": len(document)},
        )


async def _materialize_pdf(
    run_dir: Path,
    stem: str,
    *,
    semaphore: asyncio.Semaphore,
    on_event: _EventCallback | None,
) -> None:
    from scholight.pipeline.pdf_md import pdf_to_markdown

    async with semaphore:
        try:
            body = await asyncio.to_thread(pdf_to_markdown, str(run_dir / "pdfs" / f"{stem}.pdf"))
        except Exception:
            # Any conversion failure must degrade to the agent's pdftotext path.
            if on_event is not None:
                on_event("extract_failed", {"stem": stem, "source": "pdf"})
            return
    document = build_pdf_extract(body, stem=stem)
    _write_extract(run_dir / "extracts" / f"{stem}.md", document)
    if on_event is not None:
        on_event(
            "extract_written",
            {"stem": stem, "source": "pdf", "chars": len(document)},
        )


async def run_extract_pass(
    run_dir: Path,
    *,
    state: ExtractAttemptState,
    client: httpx.AsyncClient,
    on_event: _EventCallback | None = None,
    semaphore: asyncio.Semaphore | None = None,
) -> None:
    """Run one materialization pass: card-plan HTML fetches plus new PDFs."""
    if semaphore is None:
        semaphore = _extract_semaphore()
    tasks: list[asyncio.Task[None]] = []

    if not state.plan_consumed:
        plan_path = run_dir / "00_card_plan.json"
        if plan_path.is_file():
            state.plan_consumed = True
            for paper_id in planned_paper_ids(run_dir):
                stem = arxiv_artifact_stem(paper_id)
                if stem is None:
                    continue
                if stem in state.html_stems or (run_dir / "extracts" / f"{stem}.md").exists():
                    continue
                state.html_stems.add(stem)
                tasks.append(
                    asyncio.create_task(
                        _materialize_html(
                            run_dir,
                            paper_id,
                            stem,
                            client=client,
                            semaphore=semaphore,
                            on_event=on_event,
                        )
                    )
                )

    # Finish HTML materialization before considering PDF fallbacks.  Both
    # sources write the same canonical extract path; running them concurrently
    # lets a slower PDF conversion overwrite a preferred HTML extract.
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    tasks = []
    pdfs_dir = run_dir / "pdfs"
    if pdfs_dir.is_dir():
        for pdf_path in sorted(pdfs_dir.glob("*.pdf")):
            stem = pdf_path.stem
            if pdf_path.is_symlink() or not pdf_path.is_file():
                continue
            if stem in state.pdf_stems or (run_dir / "extracts" / f"{stem}.md").exists():
                continue
            state.pdf_stems.add(stem)
            tasks.append(
                asyncio.create_task(
                    _materialize_pdf(run_dir, stem, semaphore=semaphore, on_event=on_event)
                )
            )

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def materialize_extracts(
    run_dir: Path,
    *,
    stop: asyncio.Event,
    on_event: _EventCallback | None = None,
    poll_interval: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Companion task that materializes extracts until the worker stops it.

    Failures are isolated per paper and per pass: they never propagate to the
    RCM execution the way artifact observation is isolated.
    """
    state = ExtractAttemptState()
    owned_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
        )
    try:
        semaphore = _extract_semaphore()
        while not stop.is_set():
            try:
                await run_extract_pass(
                    run_dir,
                    state=state,
                    client=client,
                    on_event=on_event,
                    semaphore=semaphore,
                )
            except Exception:
                # A failing pass (e.g. transient disk error) must not kill the task.
                if on_event is not None:
                    on_event("extract_pass_failed", {})
            try:
                await asyncio.wait_for(stop.wait(), timeout=poll_interval)
            except TimeoutError:
                continue
    finally:
        if owned_client:
            await client.aclose()


__all__ = [
    "ExtractAttemptState",
    "build_html_extract",
    "build_pdf_extract",
    "html_to_markdown",
    "materialize_extracts",
    "planned_paper_ids",
    "run_extract_pass",
]
