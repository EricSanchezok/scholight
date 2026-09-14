"""S3 shards stream through one bounded read without redundant metadata requests."""

import io
from pathlib import Path

import pytest
from botocore.exceptions import ResponseStreamingError

from scholight.store.archive_io import ArchiveLocation


class Body(io.BytesIO):
    def __init__(self, data: bytes):
        super().__init__(data)
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        assert 0 < size <= 1024**2
        self.read_sizes.append(size)
        return super().read(size)


class Store:
    def __init__(self, data: bytes, size: int | None = None):
        self.body = Body(data)
        self.size = len(data) if size is None else size
        self.calls: list[dict[str, str]] = []

    def get_object(self, **kwargs):
        self.calls.append(kwargs)
        return {"Body": self.body, "ContentLength": self.size}

    def head_object(self, **kwargs):
        raise AssertionError("GET already supplies the shard length")


def test_s3_download_uses_one_get_and_bounded_reads(tmp_path: Path):
    payload = b"a" * (2 * 1024**2 + 17)
    store = Store(payload)
    output = tmp_path / "shard"
    ArchiveLocation("s3://archive/test", s3_client=store).download("part.parquet", output)
    assert output.read_bytes() == payload
    assert store.calls == [{"Bucket": "archive", "Key": "test/part.parquet"}]
    assert len(store.body.read_sizes) >= 3
    assert store.body.closed


def test_s3_download_rejects_oversize_before_reading(tmp_path: Path):
    store = Store(b"", size=300 * 1024**2 + 1)
    output = tmp_path / "shard"
    with pytest.raises(ValueError, match="300 MiB"):
        ArchiveLocation("s3://archive/test", s3_client=store).download("part", output)
    assert not store.body.read_sizes
    assert store.body.closed
    assert not output.exists()


@pytest.mark.parametrize("declared", [2, 8])
def test_s3_download_rejects_mismatched_length_without_partial_file(tmp_path: Path, declared: int):
    store = Store(b"data", size=declared)
    output = tmp_path / "shard"
    with pytest.raises(ValueError, match="length"):
        ArchiveLocation("s3://archive/test", s3_client=store).download("part", output)
    assert store.body.closed
    assert not output.exists()


def test_s3_download_removes_interrupted_partial_file(tmp_path: Path):
    class InterruptedBody(Body):
        def read(self, size: int = -1) -> bytes:
            if self.read_sizes:
                raise OSError("Interrupted stream")
            return super().read(size)

    store = Store(b"a" * (1024**2 + 1))
    store.body = InterruptedBody(b"a" * (1024**2 + 1))
    output = tmp_path / "shard"
    with pytest.raises(OSError, match="Interrupted"):
        ArchiveLocation("s3://archive/test", s3_client=store).download("part", output)
    assert store.body.closed
    assert not output.exists()


def test_s3_download_retries_stream_failure_from_start(tmp_path: Path, monkeypatch):
    class BrokenBody(Body):
        def read(self, size: int = -1) -> bytes:
            raise ResponseStreamingError(error="Connection lost")

    class RetryingStore(Store):
        def get_object(self, **kwargs):
            result = super().get_object(**kwargs)
            if len(self.calls) == 1:
                result["Body"] = broken
            return result

    broken = BrokenBody(b"data")
    store = RetryingStore(b"data")
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    output = tmp_path / "shard"
    ArchiveLocation("s3://archive/test", s3_client=store).download("part", output)
    assert output.read_bytes() == b"data"
    assert len(store.calls) == 2
    assert broken.closed and store.body.closed
