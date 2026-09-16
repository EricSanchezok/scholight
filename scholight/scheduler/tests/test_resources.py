"""Security tests for bounded arXiv source extraction."""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from scholight.scheduler.resources import (
    ResourceCorruptError,
    ResourceTemporaryError,
    _download,
    _extract_source,
    _valid_pdf,
    fetch_paper_resource,
)


def _archive(path: Path, name: str, content: bytes = b"content") -> None:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(content))


def test_source_extraction_accepts_relative_regular_file(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    destination = tmp_path / "source"
    _archive(archive, "paper/main.tex")

    _extract_source(archive, destination)

    assert (destination / "paper" / "main.tex").read_bytes() == b"content"


def test_source_extraction_rejects_parent_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    destination = tmp_path / "source"
    _archive(archive, "../escaped.tex")

    with pytest.raises(ResourceCorruptError):
        _extract_source(archive, destination)

    assert not (tmp_path / "escaped.tex").exists()


def test_source_rejection_preserves_existing_destination(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    destination = tmp_path / "source"
    destination.mkdir()
    existing = destination / "existing.tex"
    existing.write_bytes(b"previous")
    _archive(archive, "../escaped.tex")

    with pytest.raises(ResourceCorruptError):
        _extract_source(archive, destination)

    assert existing.read_bytes() == b"previous"


def test_pdf_signature_accepts_normal_pdf_header(tmp_path: Path) -> None:
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" + b"fixture" * 100)

    assert _valid_pdf(pdf) is True


def test_source_endpoint_pdf_is_recognized_without_archive_extraction(tmp_path: Path) -> None:
    def download(_url: str, destination: Path) -> int:
        destination.write_bytes(b"%PDF-1.7\n" + b"fixture" * 100)
        return 200

    with patch("scholight.scheduler.resources._download", side_effect=download) as fetch:
        resource = fetch_paper_resource("2401.00001", 1, tmp_path)

    assert resource.kind == "pdf"
    assert resource.path.name == "paper.pdf"
    assert fetch.call_count == 1


def test_invalid_source_archive_safely_falls_back_to_pdf(tmp_path: Path) -> None:
    def download(url: str, destination: Path) -> int:
        if "/src/" in url:
            destination.write_bytes(b"not a tar archive")
        else:
            destination.write_bytes(b"%PDF-1.7\n" + b"fixture" * 100)
        return 200

    with patch("scholight.scheduler.resources._download", side_effect=download) as fetch:
        resource = fetch_paper_resource("2401.00001", 1, tmp_path)

    assert resource.kind == "pdf"
    assert resource.path.name == "paper.pdf"
    assert fetch.call_count == 2


def _mock_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    client_type = httpx.Client
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr("scholight.scheduler.resources._MAX_DOWNLOAD", 1024)
    monkeypatch.setattr(
        "scholight.scheduler.resources.httpx.Client",
        lambda **kwargs: client_type(transport=transport, **kwargs),
    )


def test_oversized_source_falls_back_to_bounded_exact_version_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if "/src/" in request.url.path:
            return httpx.Response(200, content=b"x" * 1025)
        return httpx.Response(200, content=b"%PDF-1.7\n" + b"fixture" * 100)

    _mock_http(monkeypatch, respond)

    resource = fetch_paper_resource("2401.00001", 2, tmp_path)

    assert resource.kind == "pdf"
    assert resource.path.name == "paper.pdf"
    assert not (tmp_path / "source.download").exists()
    assert urls == [
        "https://arxiv.org/src/2401.00001v2",
        "https://arxiv.org/pdf/2401.00001v2.pdf",
    ]


def test_oversized_pdf_after_source_rejection_still_fails_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(200, content=b"%PDF-1.7\n" + b"x" * 1024)

    _mock_http(monkeypatch, respond)

    with pytest.raises(ResourceCorruptError, match="download limit"):
        fetch_paper_resource("2401.00001", 2, tmp_path)

    assert len(urls) == 2
    assert list(tmp_path.iterdir()) == []


def test_missing_pdf_after_oversized_source_is_not_misclassified_as_missing_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if "/src/" in request.url.path:
            return httpx.Response(200, content=b"x" * 1025)
        return httpx.Response(404)

    _mock_http(monkeypatch, respond)

    with pytest.raises(ResourceCorruptError, match="resources were invalid"):
        fetch_paper_resource("2401.00001", 2, tmp_path)


@pytest.mark.parametrize("status", [429, 503])
def test_temporary_source_error_does_not_attempt_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    urls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        return httpx.Response(status)

    _mock_http(monkeypatch, respond)

    with pytest.raises(ResourceTemporaryError):
        fetch_paper_resource("2401.00001", 2, tmp_path)

    assert urls == ["https://arxiv.org/src/2401.00001v2"]


def test_declared_oversized_download_is_rejected_before_reading_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumed: list[bool] = []

    class Body(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            consumed.append(True)
            yield b"x" * 1025

    _mock_http(
        monkeypatch,
        lambda _request: httpx.Response(200, headers={"Content-Length": "1025"}, stream=Body()),
    )

    with pytest.raises(ResourceCorruptError, match="download limit"):
        _download("https://arxiv.org/src/2401.00001v2", tmp_path / "source.download")

    assert consumed == []
    assert list(tmp_path.iterdir()) == []


def test_streaming_download_limit_removes_partial_file_without_content_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Body(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield b"x" * (1024 * 1024)
            yield b"x"

    _mock_http(monkeypatch, lambda _request: httpx.Response(200, stream=Body()))
    monkeypatch.setattr("scholight.scheduler.resources._MAX_DOWNLOAD", 1024 * 1024)
    destination = tmp_path / "source.download"

    with pytest.raises(ResourceCorruptError, match="download limit"):
        _download("https://arxiv.org/src/2401.00001v2", destination)

    assert not destination.exists()
