"""Hermetic ingestion workflow against real PostgreSQL and local substitutes."""

from __future__ import annotations

import datetime as dt
import json
import sys
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import ExitStack, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import asyncpg
import pytest
import pytest_asyncio

from scholight.config import settings
from scholight.db.client import DBError
from scholight.db.queries_ingestion import (
    enqueue_ingestion_job,
    get_ingestion_job,
    initialize_sync_cursor,
    mark_sync_started,
)
from scholight.db.tests.pg_ingestion_support import (
    isolated_database_url,
    reset_ingestion_database,
)
from scholight.pipeline.pdf_md import PDFMdError
from scholight.scheduler.ingest_worker import run_worker_once
from scholight.scheduler.metadata_sync import run_sync
from scholight.scheduler.resources import DownloadedResource, ResourceTemporaryError
from scholight.store.tests.fake_ingestion_client import FakeIngestionClient

pytestmark = pytest.mark.pg_integration
_ARXIV_ID = "2401.00001"
_SYNC_DAY = dt.date(2026, 7, 23)


@pytest_asyncio.fixture
async def ingestion_pool() -> AsyncIterator[asyncpg.Pool]:
    pool = await asyncpg.create_pool(isolated_database_url(), min_size=1, max_size=8)
    await reset_ingestion_database(pool)
    try:
        yield pool
    finally:
        await pool.close()


def _pdf_bytes() -> bytes:
    """Build a dependency-free one-page PDF with correct cross-reference offsets."""
    text = "Retrieval augmented generation combines external evidence with language models. " * 8
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 10 Tf 40 760 Td ({escaped}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode())
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode())
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode())
    output.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
        ).encode()
    )
    return bytes(output)


@pytest.fixture
def local_upstreams() -> Iterator[tuple[str, list[tuple[str, str]]]]:
    pdf = _pdf_bytes()
    requests: list[tuple[str, str]] = []
    oai_record = f"""
    <OAI-PMH><ListRecords><record>
      <header>
        <identifier>oai:arXiv.org:{_ARXIV_ID}</identifier>
        <datestamp>{_SYNC_DAY.isoformat()}</datestamp>
      </header>
      <metadata><arXivRaw>
        <title>A local ingestion fixture</title>
        <abstract>A fixed abstract used by the local embedding substitute.</abstract>
        <authors>Test Author</authors>
        <categories>cs.AI</categories>
        <version><date>{_SYNC_DAY.isoformat()}</date></version>
      </arXivRaw></metadata>
    </record></ListRecords></OAI-PMH>
    """.encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(("GET", self.path))
            if self.path.startswith("/oai"):
                payload = (
                    b"<OAI-PMH><Identify/></OAI-PMH>"
                    if "verb=Identify" in self.path
                    else oai_record
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/xml")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path.startswith("/src/"):
                self.send_response(404)
                self.end_headers()
                return
            if self.path.startswith("/pdf/"):
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Length", str(len(pdf)))
                self.end_headers()
                self.wfile.write(pdf)
                return
            self.send_response(404)
            self.end_headers()

        def do_POST(self) -> None:
            requests.append(("POST", self.path))
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length))
            inputs = body["input"]
            payload = {
                "data": [
                    {"index": index, "embedding": [float(index + 1)] * settings.embedding_dim}
                    for index, _text in enumerate(inputs)
                ]
            }
            encoded = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        raw_host, raw_port = server.server_address[:2]
        host = raw_host.decode() if isinstance(raw_host, bytes) else str(raw_host)
        port = int(raw_port)
        yield f"http://{host}:{port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _paper() -> dict[str, Any]:
    return {
        "arxiv_id": _ARXIV_ID,
        "title": "A local ingestion fixture",
        "abstract": "A fixed abstract used by the local embedding substitute.",
        "authors": ["Test Author"],
        "categories": ["cs.AI"],
        "created": _SYNC_DAY.isoformat(),
        "updated": _SYNC_DAY.isoformat(),
        "version": 1,
        "_version_available": True,
        "updated_history": [_SYNC_DAY.isoformat()],
        "license": "",
        "comments": "",
        "doi": "",
        "journal_ref": "",
        "acm_class": "",
        "has_latex": False,
        "has_pdf": False,
        "has_markdown": False,
        "has_chunks": False,
        "abstract_embedding": [],
    }


def _stored_paper(*, version: int = 1) -> dict[str, Any]:
    paper = _paper()
    paper.pop("_version_available")
    paper["version"] = version
    paper["abstract_embedding"] = [0.0] * settings.embedding_dim
    return paper


class _StubEmbedder:
    def __init__(self, *, fail: bool = False) -> None:
        self._fail = fail

    async def __aenter__(self) -> _StubEmbedder:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if self._fail:
            raise RuntimeError("injected embedding failure")
        return [[0.25] * settings.embedding_dim for _text in texts]


@pytest.mark.asyncio
async def test_full_local_workflow_reaches_succeeded_and_cleans_scratch(
    ingestion_pool: asyncpg.Pool,
    local_upstreams: tuple[str, list[tuple[str, str]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    origin, requests = local_upstreams
    fake = FakeIngestionClient()
    monkeypatch.setattr(settings, "embedding_base_url", origin)
    monkeypatch.setattr(settings, "embedding_dim", 8)
    monkeypatch.setattr(settings, "embedding_batch_size", 4)
    monkeypatch.setattr("scholight.scheduler.resources.ARXIV_SOURCE_ORIGIN", origin)
    monkeypatch.setattr("scholight.scheduler.resources.ARXIV_PDF_ORIGINS", (origin,))
    # The locked PyMuPDF wheel currently crashes the macOS test interpreter
    # during native-module import. CI and production are Linux and exercise
    # the real parser; the local macOS preflight keeps the remaining workflow
    # hermetic with the same deterministic extracted text.
    parser_context = (
        patch(
            "scholight.scheduler.ingest_worker.pdf_to_markdown",
            return_value="Local PDF extraction output. " * 40,
        )
        if sys.platform == "darwin"
        else nullcontext()
    )

    with (
        patch("scholight.db.queries_ingestion.get_pool", return_value=ingestion_pool),
        patch("scholight.store.ingestion.get_client", return_value=fake),
        patch("scholight.store.ingest.get_client", return_value=fake),
        patch("scholight.scheduler.metadata_sync.OAI_PRIMARY", f"{origin}/oai"),
        patch("scholight.scheduler.metadata_sync.OAI_FALLBACK", f"{origin}/oai"),
        parser_context,
    ):
        await mark_sync_started("arxiv")
        await initialize_sync_cursor("arxiv", _SYNC_DAY - dt.timedelta(days=1))
        sync_result = await run_sync(today=_SYNC_DAY + dt.timedelta(days=1))
        pending = await get_ingestion_job(_ARXIV_ID)
        worked = await run_worker_once("local-worker", scratch_root=tmp_path)
        completed = await get_ingestion_job(_ARXIV_ID)

    assert sync_result["failed_date"] is None
    assert pending is not None and pending.status == "pending"
    assert worked is True
    assert completed is not None and completed.status == "succeeded"
    assert fake.papers[_ARXIV_ID]["has_chunks"] is True
    assert fake.chunks
    assert list(tmp_path.iterdir()) == []
    assert any(method == "GET" and path.startswith("/oai?") for method, path in requests)
    assert ("GET", f"/pdf/{_ARXIV_ID}v1.pdf") in requests
    assert sum(method == "POST" and path == "/embeddings" for method, path in requests) >= 2


@pytest.mark.asyncio
async def test_revision_job_is_recovered_after_zilliz_then_postgres_failure(
    ingestion_pool: asyncpg.Pool,
) -> None:
    fake = FakeIngestionClient()
    fake.papers[_ARXIV_ID] = {
        **_stored_paper(),
        "version": 1,
        "has_chunks": True,
    }
    revision = {
        **_paper(),
        "version": 2,
        "updated": _SYNC_DAY.isoformat(),
        "updated_history": ["2026-07-01", _SYNC_DAY.isoformat()],
    }

    with (
        patch("scholight.db.queries_ingestion.get_pool", return_value=ingestion_pool),
        patch("scholight.store.ingestion.get_client", return_value=fake),
        patch(
            "scholight.scheduler.metadata_sync._fetch_day",
            AsyncMock(return_value=([revision], "oai")),
        ),
        patch("scholight.scheduler.metadata_sync._normalize_and_embed", AsyncMock()),
    ):
        await mark_sync_started("arxiv")
        await initialize_sync_cursor("arxiv", _SYNC_DAY - dt.timedelta(days=1))
        with patch(
            "scholight.scheduler.metadata_sync.enqueue_ingestion_job",
            AsyncMock(side_effect=DBError("injected postgres outage")),
        ):
            interrupted = await run_sync(today=_SYNC_DAY + dt.timedelta(days=1))
        after_interruption = await get_ingestion_job(_ARXIV_ID)
        recovered = await run_sync(today=_SYNC_DAY + dt.timedelta(days=1))
        job = await get_ingestion_job(_ARXIV_ID)

    assert interrupted["failed_date"] == _SYNC_DAY.isoformat()
    assert after_interruption is None
    assert recovered["failed_date"] is None
    assert job is not None
    assert (job.target_version, job.source, job.status) == (2, "revision", "pending")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage",
    ["download", "parse", "chunk", "embedding", "zilliz", "completion"],
)
async def test_stage_failure_retries_without_losing_or_duplicating_job(
    stage: str,
    ingestion_pool: asyncpg.Pool,
    tmp_path: Path,
) -> None:
    fake = FakeIngestionClient()
    fake.papers[_ARXIV_ID] = _stored_paper()
    fake.chunks["old-chunk"] = {
        "chunk_id": "old-chunk",
        "arxiv_id": _ARXIV_ID,
        "chunk_idx": 99,
        "content_text": "old",
        "content_embedding": [0.0] * settings.embedding_dim,
    }
    resource = tmp_path / "fixture.pdf"
    resource.write_bytes(b"%PDF-local-fixture")
    markdown = "A complete deterministic paragraph for ingestion testing. " * 30
    if stage == "zilliz":
        fake.fail_next_chunk_upsert = True

    with patch("scholight.db.queries_ingestion.get_pool", return_value=ingestion_pool):
        await enqueue_ingestion_job(_ARXIV_ID, 1, "new", max_attempts=8)
        with ExitStack() as stack:
            stack.enter_context(patch("scholight.store.ingestion.get_client", return_value=fake))
            stack.enter_context(
                patch(
                    "scholight.scheduler.ingest_worker.fetch_paper_resource",
                    side_effect=(
                        ResourceTemporaryError("injected download failure")
                        if stage == "download"
                        else None
                    ),
                    return_value=DownloadedResource("pdf", resource),
                )
            )
            stack.enter_context(
                patch(
                    "scholight.scheduler.ingest_worker.pdf_to_markdown",
                    side_effect=(
                        PDFMdError("injected parse failure") if stage == "parse" else None
                    ),
                    return_value=markdown,
                )
            )
            if stage == "chunk":
                stack.enter_context(
                    patch("scholight.scheduler.ingest_worker.chunk_markdown", return_value=[])
                )
            stack.enter_context(
                patch(
                    "scholight.scheduler.ingest_worker.Embedder",
                    return_value=_StubEmbedder(fail=stage == "embedding"),
                )
            )
            if stage == "completion":
                stack.enter_context(
                    patch(
                        "scholight.scheduler.ingest_worker.complete_ingestion_job",
                        AsyncMock(side_effect=DBError("injected completion failure")),
                    )
                )
            await run_worker_once("worker-first", scratch_root=tmp_path / "scratch")

        retry = await get_ingestion_job(_ARXIV_ID)
        count_after_failure = await ingestion_pool.fetchval(
            "SELECT count(*) FROM scholight.ingestion_jobs WHERE arxiv_id = $1",
            _ARXIV_ID,
        )
        deletes_after_failure = [
            operation for operation in fake.operations if operation[0] == "delete"
        ]
        await ingestion_pool.execute(
            "UPDATE scholight.ingestion_jobs SET available_at = now() - interval '1 second' "
            "WHERE arxiv_id = $1",
            _ARXIV_ID,
        )
        with (
            patch("scholight.store.ingestion.get_client", return_value=fake),
            patch(
                "scholight.scheduler.ingest_worker.fetch_paper_resource",
                return_value=DownloadedResource("pdf", resource),
            ),
            patch(
                "scholight.scheduler.ingest_worker.pdf_to_markdown",
                return_value=markdown,
            ),
            patch(
                "scholight.scheduler.ingest_worker.Embedder",
                return_value=_StubEmbedder(),
            ),
        ):
            await run_worker_once("worker-retry", scratch_root=tmp_path / "scratch")
        succeeded = await get_ingestion_job(_ARXIV_ID)

    assert retry is not None and retry.status == "retry"
    assert count_after_failure == 1
    if stage != "completion":
        assert deletes_after_failure == []
    assert succeeded is not None and succeeded.status == "succeeded"
    assert len(fake.chunks) == 1
    assert list((tmp_path / "scratch").iterdir()) == []
