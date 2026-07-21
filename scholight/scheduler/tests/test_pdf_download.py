"""Security tests for arXiv source archive extraction."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from scholight.scheduler import pdf_download
from scholight.scheduler.pdf_download import _extract_tarball


def _write_archive(path: Path, member_name: str, content: bytes = b"content") -> None:
    member = tarfile.TarInfo(member_name)
    member.size = len(content)
    with tarfile.open(path, "w:gz") as archive:
        archive.addfile(member, io.BytesIO(content))


def _write_members(path: Path, members: list[tuple[str, bytes]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))


def test_extract_tarball_accepts_regular_relative_file(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    destination = tmp_path / "source"
    _write_archive(archive, "paper/main.tex")

    result = _extract_tarball(archive, destination)

    assert result is True
    assert (destination / "paper" / "main.tex").read_bytes() == b"content"


def test_extract_tarball_rejects_parent_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "malicious.tar.gz"
    destination = tmp_path / "source"
    escaped = tmp_path / "escaped.tex"
    _write_archive(archive, "../escaped.tex")

    result = _extract_tarball(archive, destination)

    assert result is False
    assert not escaped.exists()


def test_extract_tarball_rejects_oversized_member_and_removes_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "oversized.tar.gz"
    destination = tmp_path / "source"
    _write_archive(archive, "large.tex", b"12345")
    monkeypatch.setattr(pdf_download, "_MAX_ARCHIVE_MEMBER_BYTES", 4)

    result = _extract_tarball(archive, destination)

    assert result is False
    assert not destination.exists()
    assert not destination.with_name("source.extracting").exists()


def test_extract_tarball_rejects_excessive_total_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "total.tar.gz"
    destination = tmp_path / "source"
    _write_members(archive, [("one.tex", b"123"), ("two.tex", b"456")])
    monkeypatch.setattr(pdf_download, "_MAX_ARCHIVE_TOTAL_BYTES", 5)

    result = _extract_tarball(archive, destination)

    assert result is False
    assert not destination.exists()


def test_extract_tarball_rejects_excessive_member_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "members.tar.gz"
    destination = tmp_path / "source"
    _write_members(archive, [("one.tex", b"1"), ("two.tex", b"2")])
    monkeypatch.setattr(pdf_download, "_MAX_ARCHIVE_MEMBERS", 1)

    result = _extract_tarball(archive, destination)

    assert result is False
    assert not destination.exists()


def test_extract_tarball_rejects_symbolic_link(tmp_path: Path) -> None:
    archive = tmp_path / "link.tar.gz"
    destination = tmp_path / "source"
    member = tarfile.TarInfo("link.tex")
    member.type = tarfile.SYMTYPE
    member.linkname = "target.tex"
    with tarfile.open(archive, "w:gz") as tar:
        tar.addfile(member)

    result = _extract_tarball(archive, destination)

    assert result is False
    assert not destination.exists()


def test_extract_tarball_rejects_hard_link(tmp_path: Path) -> None:
    archive = tmp_path / "hard-link.tar.gz"
    destination = tmp_path / "source"
    member = tarfile.TarInfo("link.tex")
    member.type = tarfile.LNKTYPE
    member.linkname = "target.tex"
    with tarfile.open(archive, "w:gz") as tar:
        tar.addfile(member)

    result = _extract_tarball(archive, destination)

    assert result is False
    assert not destination.exists()


def test_extract_tarball_rejects_device_entry(tmp_path: Path) -> None:
    archive = tmp_path / "device.tar.gz"
    destination = tmp_path / "source"
    member = tarfile.TarInfo("device")
    member.type = tarfile.CHRTYPE
    with tarfile.open(archive, "w:gz") as tar:
        tar.addfile(member)

    result = _extract_tarball(archive, destination)

    assert result is False
    assert not destination.exists()


def test_extract_tarball_rejection_preserves_existing_destination(tmp_path: Path) -> None:
    archive = tmp_path / "malicious.tar.gz"
    destination = tmp_path / "source"
    destination.mkdir()
    existing = destination / "existing.tex"
    existing.write_bytes(b"previous-version")
    _write_archive(archive, "../escaped.tex")

    result = _extract_tarball(archive, destination)

    assert result is False
    assert existing.read_bytes() == b"previous-version"


def test_extract_tarball_counts_pax_metadata_in_stream_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = tmp_path / "pax-metadata.tar.gz"
    destination = tmp_path / "source"
    content = b"ok"
    member = tarfile.TarInfo("paper.tex")
    member.size = len(content)
    member.pax_headers = {"comment": "x" * 4096}
    with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        tar.addfile(member, io.BytesIO(content))
    monkeypatch.setattr(pdf_download, "_MAX_ARCHIVE_STREAM_BYTES", 1024)

    result = _extract_tarball(archive, destination)

    assert result is False
    assert not destination.exists()
    assert not destination.with_name("source.extracting").exists()
