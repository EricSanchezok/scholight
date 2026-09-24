from __future__ import annotations

from pathlib import Path

import pytest

from scholight.web_extract.errors import ExtractError
from scholight.web_extract.spool import Spool


def test_spool_enforces_total_reserved_bytes_and_releases_files(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=10)
    spool.start()
    with spool.allocate(6) as first:
        first.write(b"abc")
        with pytest.raises(ExtractError, match="scratch capacity"):
            spool.allocate(5)
        assert first.path.read_bytes() == b"abc"
    assert not first.path.exists()
    assert spool.reserved_bytes == 0
    spool.close()


def test_spool_cleans_only_owned_stale_files_at_startup(tmp_path: Path) -> None:
    owned = tmp_path / "extract-stale.tmp"
    other = tmp_path / "unrelated.txt"
    owned.write_bytes(b"old")
    other.write_text("keep")
    spool = Spool(tmp_path, max_bytes=10)
    spool.start()
    assert not owned.exists()
    assert other.exists()
    spool.close()


def test_spool_rejects_oversized_stream_and_cleans_failure(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=10)
    spool.start()
    with pytest.raises(ExtractError, match="scratch file limit"), spool.allocate(4) as body:
        body.write(b"12345")
    assert spool.reserved_bytes == 0
    assert not list(tmp_path.glob("extract-*.tmp"))
    spool.close()


def test_second_supervisor_cannot_clean_an_active_spool(tmp_path: Path) -> None:
    first = Spool(tmp_path, max_bytes=10)
    first.start()
    second = Spool(tmp_path, max_bytes=10)
    with first.allocate(4) as body:
        body.write(b"1234")
        with pytest.raises(BlockingIOError):
            second.start()
        assert body.path.read_bytes() == b"1234"
    first.close()


def test_sealing_a_download_releases_unused_disk_reservation(tmp_path: Path) -> None:
    spool = Spool(tmp_path, max_bytes=10)
    spool.start()
    try:
        with spool.allocate(10) as body:
            body.write(b"abc")
            body.seal()
            body.seal()
            assert spool.reserved_bytes == 3
            with spool.allocate(7):
                with pytest.raises(ExtractError):
                    body.write(b"more")
        assert spool.reserved_bytes == 0
    finally:
        spool.close()
