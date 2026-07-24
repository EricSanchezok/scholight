"""Security tests for bounded arXiv source extraction."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from scholight.scheduler.resources import (
    ResourceCorruptError,
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
