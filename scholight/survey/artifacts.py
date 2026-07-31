"""Private, owner-scoped Survey run archiving in S3."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID

import boto3
import structlog

logger = structlog.get_logger(__name__)

_READ_CHUNK_BYTES = 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class SurveyArtifactError(Exception):
    """A Survey run could not be safely archived or retrieved."""


@dataclass(frozen=True, slots=True)
class SurveyArchive:
    storage_prefix: str
    manifest_key: str
    manifest: dict[str, Any]


def _sha256_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(_READ_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _content_type(path: Path) -> str:
    guessed, _encoding = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def _safe_files(run_root: Path) -> tuple[list[Path], list[str]]:
    resolved_root = run_root.resolve(strict=True)
    if not resolved_root.is_dir():
        raise SurveyArtifactError("Survey run workspace is not a directory")
    files: list[Path] = []
    excluded: list[str] = []
    for directory, dirnames, filenames in os.walk(resolved_root, followlinks=False):
        base = Path(directory)
        safe_dirs: list[str] = []
        for dirname in sorted(dirnames):
            candidate = base / dirname
            relative = candidate.relative_to(resolved_root).as_posix()
            if candidate.is_symlink():
                excluded.append(relative)
            else:
                safe_dirs.append(dirname)
        dirnames[:] = safe_dirs
        for filename in sorted(filenames):
            candidate = base / filename
            relative = candidate.relative_to(resolved_root).as_posix()
            file_stat = candidate.lstat()
            if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
                excluded.append(relative)
                continue
            files.append(candidate)
    files.sort(key=lambda item: item.relative_to(resolved_root).as_posix())
    excluded.sort()
    return files, excluded


def _validated_records(
    manifest: dict[str, Any],
    *,
    manifest_key: str,
) -> list[dict[str, Any]]:
    parts = manifest_key.split("/")
    if len(parts) != 5 or parts[:2] != ["surveys", "v1"] or parts[4] != "manifest.json":
        raise SurveyArtifactError("Survey artifact manifest key is invalid")
    try:
        expected_user_id = int(parts[2])
        expected_job_id = UUID(parts[3])
    except (TypeError, ValueError) as exc:
        raise SurveyArtifactError("Survey artifact manifest key is invalid") from exc
    if manifest.get("user_id") != expected_user_id or manifest.get("job_id") != str(
        expected_job_id
    ):
        raise SurveyArtifactError("Survey artifact manifest ownership is invalid")

    prefix = "/".join(parts[:4])
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in manifest["files"]:
        if not isinstance(record, dict):
            raise SurveyArtifactError("Survey artifact manifest entry is invalid")
        key = record.get("key")
        path = record.get("path")
        size = record.get("size")
        sha256 = record.get("sha256")
        mime = record.get("mime")
        path_parts = PurePosixPath(path).parts if isinstance(path, str) else ()
        expected_key = f"{prefix}/{path}" if isinstance(path, str) else None
        if (
            not isinstance(key, str)
            or key != expected_key
            or key == manifest_key
            or key in seen
            or not isinstance(path, str)
            or not path
            or path.startswith("/")
            or ".." in path_parts
            or (path != "run.json" and (not path_parts or path_parts[0] != "run"))
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(sha256, str)
            or _SHA256_PATTERN.fullmatch(sha256) is None
            or not isinstance(mime, str)
        ):
            raise SurveyArtifactError("Survey artifact manifest entry is invalid")
        seen.add(key)
        records.append(record)
    return records


class SurveyArtifactStore:
    """Archive complete Survey workspaces without following unsafe filesystem entries."""

    def __init__(self, *, bucket: str, client: Any | None = None) -> None:
        if not bucket.strip():
            raise ValueError("Survey artifact bucket is required")
        self._bucket = bucket
        self._client = client or boto3.client("s3")

    @staticmethod
    def prefix(*, user_id: int, job_id: UUID) -> str:
        return f"surveys/v1/{user_id}/{job_id}"

    async def archive_run(
        self,
        *,
        user_id: int,
        job_id: UUID,
        run_root: Path,
        run_metadata: dict[str, Any],
    ) -> SurveyArchive:
        """Upload regular files, then the run record, and the manifest last."""
        return await asyncio.to_thread(
            self._archive_run_sync,
            user_id=user_id,
            job_id=job_id,
            run_root=run_root,
            run_metadata=run_metadata,
        )

    def _archive_run_sync(
        self,
        *,
        user_id: int,
        job_id: UUID,
        run_root: Path,
        run_metadata: dict[str, Any],
    ) -> SurveyArchive:
        resolved_root = run_root.resolve(strict=True)
        files, excluded = _safe_files(resolved_root)
        storage_prefix = self.prefix(user_id=user_id, job_id=job_id)
        records: list[dict[str, Any]] = []
        for path in files:
            relative = path.relative_to(resolved_root).as_posix()
            key = f"{storage_prefix}/run/{relative}"
            content_type = _content_type(path)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags)
            except OSError as exc:
                raise SurveyArtifactError("Survey artifact changed during archiving") from exc
            with os.fdopen(descriptor, "rb") as stream:
                file_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(file_stat.st_mode):
                    raise SurveyArtifactError("Survey artifact is not a regular file")
                sha256 = _sha256_stream(stream)
                stream.seek(0)
                self._client.upload_fileobj(
                    stream,
                    self._bucket,
                    key,
                    ExtraArgs={"ContentType": content_type},
                )
                size = file_stat.st_size
            self._verify_size(key=key, expected=size)
            records.append(
                {
                    "path": f"run/{relative}",
                    "key": key,
                    "size": size,
                    "sha256": sha256,
                    "mime": content_type,
                }
            )

        run_key = f"{storage_prefix}/run.json"
        run_body = json.dumps(
            run_metadata,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._client.put_object(
            Bucket=self._bucket,
            Key=run_key,
            Body=run_body,
            ContentType="application/json",
        )
        self._verify_size(key=run_key, expected=len(run_body))
        records.append(
            {
                "path": "run.json",
                "key": run_key,
                "size": len(run_body),
                "sha256": hashlib.sha256(run_body).hexdigest(),
                "mime": "application/json",
            }
        )

        manifest_key = f"{storage_prefix}/manifest.json"
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "job_id": str(job_id),
            "user_id": user_id,
            "files": records,
            "excluded_unsafe_paths": excluded,
        }
        manifest_body = json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        self._client.put_object(
            Bucket=self._bucket,
            Key=manifest_key,
            Body=manifest_body,
            ContentType="application/json",
        )
        self._verify_size(key=manifest_key, expected=len(manifest_body))
        logger.info(
            "survey_artifacts_archived",
            job_id=str(job_id),
            file_count=len(records),
            excluded_count=len(excluded),
        )
        return SurveyArchive(
            storage_prefix=storage_prefix,
            manifest_key=manifest_key,
            manifest=manifest,
        )

    def _verify_size(self, *, key: str, expected: int) -> None:
        response = self._client.head_object(Bucket=self._bucket, Key=key)
        if int(response["ContentLength"]) != expected:
            raise SurveyArtifactError("Survey artifact size verification failed")

    async def read_manifest(self, *, manifest_key: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._read_manifest_sync, manifest_key)

    def _read_manifest_sync(self, manifest_key: str) -> dict[str, Any]:
        response = self._client.get_object(Bucket=self._bucket, Key=manifest_key)
        payload = json.load(response["Body"])
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("files"), list)
        ):
            raise SurveyArtifactError("Survey artifact manifest is invalid")
        return payload

    async def presigned_artifacts(
        self,
        *,
        manifest_key: str,
        expires_seconds: int = 300,
    ) -> list[dict[str, Any]]:
        manifest = await self.read_manifest(manifest_key=manifest_key)
        return await asyncio.to_thread(
            self._presigned_artifacts_sync,
            manifest,
            manifest_key,
            expires_seconds,
        )

    def _presigned_artifacts_sync(
        self,
        manifest: dict[str, Any],
        manifest_key: str,
        expires_seconds: int,
    ) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for record in _validated_records(manifest, manifest_key=manifest_key):
            artifacts.append(
                {
                    **record,
                    "url": self._client.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": self._bucket, "Key": record["key"]},
                        ExpiresIn=expires_seconds,
                    ),
                }
            )
        return artifacts

    async def delete_archive(self, *, manifest_key: str, preserve_manifest: bool = False) -> None:
        manifest = await self.read_manifest(manifest_key=manifest_key)
        await asyncio.to_thread(
            self._delete_archive_sync,
            manifest,
            manifest_key,
            preserve_manifest,
        )

    def _delete_archive_sync(
        self,
        manifest: dict[str, Any],
        manifest_key: str,
        preserve_manifest: bool,
    ) -> None:
        keys = [record["key"] for record in _validated_records(manifest, manifest_key=manifest_key)]
        if not preserve_manifest:
            keys.append(manifest_key)
        for offset in range(0, len(keys), 1000):
            batch = keys[offset : offset + 1000]
            response = self._client.delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
            )
            if response.get("Errors"):
                raise SurveyArtifactError("Survey artifact deletion was incomplete")

    async def delete_manifest(self, *, manifest_key: str) -> None:
        """Delete the retained manifest after the owner-scoped database row is gone."""
        await asyncio.to_thread(
            self._client.delete_object,
            Bucket=self._bucket,
            Key=manifest_key,
        )


__all__ = [
    "SurveyArchive",
    "SurveyArtifactError",
    "SurveyArtifactStore",
]
