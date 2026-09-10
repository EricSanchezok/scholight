"""Bounded local/S3 archive IO. Credentials stay in the AWS provider chain."""

from __future__ import annotations

import fcntl
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from botocore.exceptions import ClientError


def file_digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


class ArchiveLocation:
    """An explicit archive root; object names never escape it."""

    def __init__(self, location: str, *, s3_client: Any = None) -> None:
        self._versions: dict[str, str] = {}
        parsed = urlsplit(location)
        if parsed.scheme == "s3":
            if not parsed.netloc or not parsed.path.strip("/") or parsed.query or parsed.fragment:
                raise ValueError("S3 archive requires a bucket and a dedicated prefix")
            import boto3

            self.s3 = s3_client or boto3.client("s3")
            self.bucket = parsed.netloc
            self.prefix = parsed.path.strip("/")
            self.root: Path | None = None
        elif not parsed.scheme:
            self.root = Path(location).resolve()
            self.s3 = None
            self.bucket = self.prefix = ""
        else:
            raise ValueError("Archive location must be a local directory or s3://bucket/prefix")

    def _key(self, name: str) -> str:
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError("Invalid archive object name")
        return f"{self.prefix}/{name}"

    def exists(self, name: str) -> bool:
        key = self._key(name)
        if self.root is not None:
            return (self.root / name).exists()
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def upload(self, path: Path, name: str) -> None:
        key = self._key(name)
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary = self.root / f".{uuid4().hex}.partial"
            try:
                shutil.copyfile(path, temporary)
                temporary.replace(self.root / name)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            from boto3.s3.transfer import TransferConfig

            self.s3.upload_file(
                str(path),
                self.bucket,
                key,
                ExtraArgs={"ServerSideEncryption": "AES256"},
                Config=TransferConfig(max_concurrency=1, use_threads=False),
            )

    def download(self, name: str, path: Path) -> None:
        key = self._key(name)
        if self.root is not None:
            if (self.root / name).stat().st_size > 300 * 1024**2:
                raise ValueError("Archive shard exceeds the 300 MiB download limit")
            shutil.copyfile(self.root / name, path)
        else:
            from boto3.s3.transfer import TransferConfig

            if self.s3.head_object(Bucket=self.bucket, Key=key)["ContentLength"] > 300 * 1024**2:
                raise ValueError("Archive shard exceeds the 300 MiB download limit")
            self.s3.download_file(
                self.bucket,
                key,
                str(path),
                Config=TransferConfig(max_concurrency=1, use_threads=False),
            )

    def read_json(self, name: str) -> dict[str, Any]:
        key = self._key(name)
        if self.root is not None:
            with (self.root / name).open("rb") as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            self._versions[name] = hashlib.sha256(raw).hexdigest()
        else:
            response = self.s3.get_object(Bucket=self.bucket, Key=key)
            self._versions[name] = response["ETag"]
            try:
                raw = response["Body"].read(16 * 1024 * 1024 + 1)
            finally:
                response["Body"].close()
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Archive metadata exceeds 16 MiB")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Archive metadata must be an object")
        return result

    def write_json(self, name: str, payload: dict[str, Any], workspace: Path) -> None:
        """Compare-and-swap metadata: concurrent writers cannot overwrite progress."""
        key = self._key(name)
        path = workspace / "metadata.json"
        path.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False))
        expected = self._versions.get(name)
        if self.root is not None:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / ".archive.lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                target = self.root / name
                actual = file_digest(target) if target.exists() else None
                if actual != expected:
                    raise ValueError(
                        "Concurrent archive metadata update; restart from committed progress"
                    )
                self.upload(path, name)
                self._versions[name] = file_digest(path)
        else:
            condition = {"IfMatch": expected} if expected else {"IfNoneMatch": "*"}
            with path.open("rb") as stream:
                response = self.s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=stream,
                    ContentType="application/json",
                    ServerSideEncryption="AES256",
                    **condition,
                )
            self._versions[name] = response["ETag"]
