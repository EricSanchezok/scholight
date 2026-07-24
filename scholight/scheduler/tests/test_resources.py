"""Security tests for bounded arXiv source extraction."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from scholight.scheduler.resources import ResourceCorruptError, _extract_source


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
