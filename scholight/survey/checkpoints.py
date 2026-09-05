"""Immutable, content-addressed S3 checkpoints for one Survey workspace."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

_READ_CHUNK_BYTES = 1024 * 1024
_MANIFEST_MAX_BYTES = 8 * 1024 * 1024
_FILE_MAX_BYTES = 512 * 1024 * 1024
_WORKSPACE_MAX_BYTES = 4 * 1024 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_ROOT_SUFFIXES = frozenset({".md", ".json", ".pdf", ".png", ".jpg", ".jpeg", ".webp"})
_DURABLE_DIRECTORIES = frozenset(
    {
        "cards",
        "sections",
        "pdfs",
        "extracts",
        "images",
        "charts",
        "assets",
        "reference_inputs",
        "reference_results",
        "shard_results",
    }
)
_EXCLUDED_NAMES = frozenset({"diagnostics.json", "trajectory.json", "trajectory.jsonl"})


def _s3_client(endpoint_url: str | None) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        config=Config(
            signature_version="s3v4",
            connect_timeout=3,
            read_timeout=60,
            retries={"max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": "path" if endpoint_url else "auto"},
        ),
    )


class SurveyCheckpointError(Exception):
    """A workspace checkpoint was unsafe, corrupt, or could not be published."""


@dataclass(frozen=True, slots=True)
class SurveyCheckpoint:
    sequence: int
    stage: str
    manifest_key: str
    manifest_sha256: str
    completed_units: tuple[str, ...]
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _LocalFile:
    path: Path
    relative: str
    size: int
    sha256: str


def _durable_relative_path(relative: str) -> bool:
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or ".." in path.parts or "." in path.parts:
        return False
    if any(part.startswith(".") for part in path.parts):
        return False
    if path.name in _EXCLUDED_NAMES:
        return False
    if len(path.parts) == 1:
        return path.suffix.lower() in _ROOT_SUFFIXES
    return path.parts[0] in _DURABLE_DIRECTORIES


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(_READ_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _local_files(run_root: Path) -> list[_LocalFile]:
    resolved = run_root.resolve(strict=True)
    if not resolved.is_dir() or run_root.is_symlink():
        raise SurveyCheckpointError("Survey checkpoint workspace is invalid")
    records: list[_LocalFile] = []
    total = 0
    for directory, dirnames, filenames in os.walk(resolved, followlinks=False):
        base = Path(directory)
        safe_directories: list[str] = []
        for dirname in sorted(dirnames):
            candidate = base / dirname
            relative = candidate.relative_to(resolved).as_posix()
            if candidate.is_symlink():
                raise SurveyCheckpointError("Survey checkpoint workspace contains a symlink")
            if len(PurePosixPath(relative).parts) == 1 and dirname not in _DURABLE_DIRECTORIES:
                continue
            safe_directories.append(dirname)
        dirnames[:] = safe_directories
        for filename in sorted(filenames):
            candidate = base / filename
            relative = candidate.relative_to(resolved).as_posix()
            if not _durable_relative_path(relative):
                continue
            metadata = candidate.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise SurveyCheckpointError("Survey checkpoint workspace contains a non-file")
            if metadata.st_size > _FILE_MAX_BYTES:
                raise SurveyCheckpointError("Survey checkpoint file exceeds the size limit")
            total += metadata.st_size
            if total > _WORKSPACE_MAX_BYTES:
                raise SurveyCheckpointError("Survey checkpoint workspace exceeds the size limit")
            records.append(
                _LocalFile(
                    path=candidate,
                    relative=relative,
                    size=metadata.st_size,
                    sha256=_hash_file(candidate),
                )
            )
    records.sort(key=lambda item: item.relative)
    return records


def _read_body(body: Any, *, maximum: int) -> bytes:
    output = bytearray()
    try:
        while chunk := body.read(_READ_CHUNK_BYTES):
            output.extend(chunk)
            if len(output) > maximum:
                raise SurveyCheckpointError("Survey checkpoint object exceeds the size limit")
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    return bytes(output)


class SurveyCheckpointStore:
    """Publish manifests last and reconstruct a verified workspace from S3."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        client: Any | None = None,
    ) -> None:
        if not bucket.strip():
            raise ValueError("Survey checkpoint bucket is required")
        self._bucket = bucket
        self._client = client or _s3_client(endpoint_url)

    @staticmethod
    def prefix(*, user_id: int, job_id: UUID) -> str:
        if user_id < 1:
            raise ValueError("Survey checkpoint user ID must be positive")
        return f"surveys/_checkpoints/v1/{user_id}/{job_id}"

    async def publish(
        self,
        *,
        user_id: int,
        job_id: UUID,
        run_root: Path,
        sequence: int,
        stage: str,
        completed_units: tuple[str, ...],
        workflow_version: str,
        executor_version: str,
        parent_manifest_sha256: str | None,
    ) -> SurveyCheckpoint:
        return await asyncio.to_thread(
            self._publish_sync,
            user_id=user_id,
            job_id=job_id,
            run_root=run_root,
            sequence=sequence,
            stage=stage,
            completed_units=completed_units,
            workflow_version=workflow_version,
            executor_version=executor_version,
            parent_manifest_sha256=parent_manifest_sha256,
        )

    def _publish_sync(
        self,
        *,
        user_id: int,
        job_id: UUID,
        run_root: Path,
        sequence: int,
        stage: str,
        completed_units: tuple[str, ...],
        workflow_version: str,
        executor_version: str,
        parent_manifest_sha256: str | None,
    ) -> SurveyCheckpoint:
        if sequence < 1:
            raise ValueError("Survey checkpoint sequence must be positive")
        values = (stage, workflow_version, executor_version)
        if any(_NAME.fullmatch(value) is None for value in values):
            raise ValueError("Survey checkpoint version or stage is invalid")
        units = tuple(sorted(set(completed_units)))
        if any(_NAME.fullmatch(unit) is None for unit in units):
            raise ValueError("Survey checkpoint completed unit is invalid")
        if parent_manifest_sha256 is not None and _SHA256.fullmatch(parent_manifest_sha256) is None:
            raise ValueError("Survey checkpoint parent hash is invalid")
        prefix = self.prefix(user_id=user_id, job_id=job_id)
        files = _local_files(run_root)
        manifest_files: list[dict[str, object]] = []
        for record in files:
            object_key = f"{prefix}/objects/{record.sha256}"
            self._put_file_once(record, object_key=object_key)
            manifest_files.append(
                {
                    "path": record.relative,
                    "object_key": object_key,
                    "size": record.size,
                    "sha256": record.sha256,
                }
            )
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "user_id": user_id,
            "job_id": str(job_id),
            "sequence": sequence,
            "stage": stage,
            "workflow_version": workflow_version,
            "executor_version": executor_version,
            "parent_manifest_sha256": parent_manifest_sha256,
            "completed_units": list(units),
            "files": manifest_files,
        }
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(payload) > _MANIFEST_MAX_BYTES:
            raise SurveyCheckpointError("Survey checkpoint manifest exceeds the size limit")
        manifest_sha256 = hashlib.sha256(payload).hexdigest()
        manifest_key = f"{prefix}/manifests/{sequence:08d}-{manifest_sha256}.json"
        self._put_bytes_once(payload, key=manifest_key, content_type="application/json")
        verified = self._get_bytes(manifest_key, maximum=_MANIFEST_MAX_BYTES)
        if verified != payload:
            raise SurveyCheckpointError("Survey checkpoint manifest read-back failed")
        return SurveyCheckpoint(
            sequence=sequence,
            stage=stage,
            manifest_key=manifest_key,
            manifest_sha256=manifest_sha256,
            completed_units=units,
            manifest=manifest,
        )

    def _put_file_once(self, record: _LocalFile, *, object_key: str) -> None:
        try:
            with record.path.open("rb") as source:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=object_key,
                    Body=source,
                    ContentType="application/octet-stream",
                    IfNoneMatch="*",
                )
        except ClientError as exc:
            if not _is_precondition_failed(exc):
                raise SurveyCheckpointError("Survey checkpoint object upload failed") from exc
            existing = self._get_bytes(object_key, maximum=record.size)
            if (
                len(existing) != record.size
                or hashlib.sha256(existing).hexdigest() != record.sha256
            ):
                raise SurveyCheckpointError("Survey checkpoint object hash is invalid") from exc

    def _put_bytes_once(self, payload: bytes, *, key: str, content_type: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=payload,
                ContentType=content_type,
                IfNoneMatch="*",
            )
        except ClientError as exc:
            if not _is_precondition_failed(exc):
                raise SurveyCheckpointError("Survey checkpoint manifest upload failed") from exc
            if self._get_bytes(key, maximum=len(payload)) != payload:
                raise SurveyCheckpointError("Survey checkpoint manifest conflict") from exc

    def _get_bytes(self, key: str, *, maximum: int) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return _read_body(response["Body"], maximum=maximum)
        except SurveyCheckpointError:
            raise
        except Exception as exc:
            raise SurveyCheckpointError("Survey checkpoint object download failed") from exc

    async def restore(
        self,
        *,
        user_id: int,
        job_id: UUID,
        run_root: Path,
        manifest_key: str,
        manifest_sha256: str,
    ) -> SurveyCheckpoint:
        return await asyncio.to_thread(
            self._restore_sync,
            user_id=user_id,
            job_id=job_id,
            run_root=run_root,
            manifest_key=manifest_key,
            manifest_sha256=manifest_sha256,
        )

    async def find_successor(
        self,
        *,
        user_id: int,
        job_id: UUID,
        expected_sequence: int,
        parent_manifest_sha256: str | None,
        workflow_version: str,
        executor_version: str,
    ) -> SurveyCheckpoint | None:
        """Find one manifest uploaded immediately before a crashed database CAS."""
        return await asyncio.to_thread(
            self._find_successor_sync,
            user_id=user_id,
            job_id=job_id,
            expected_sequence=expected_sequence,
            parent_manifest_sha256=parent_manifest_sha256,
            workflow_version=workflow_version,
            executor_version=executor_version,
        )

    def _find_successor_sync(
        self,
        *,
        user_id: int,
        job_id: UUID,
        expected_sequence: int,
        parent_manifest_sha256: str | None,
        workflow_version: str,
        executor_version: str,
    ) -> SurveyCheckpoint | None:
        prefix = self.prefix(user_id=user_id, job_id=job_id)
        try:
            response = self._client.list_objects_v2(
                Bucket=self._bucket,
                Prefix=f"{prefix}/manifests/{expected_sequence:08d}-",
                MaxKeys=10,
            )
        except Exception as exc:
            raise SurveyCheckpointError("Survey checkpoint successor lookup failed") from exc
        contents = response.get("Contents", [])
        if not isinstance(contents, list):
            raise SurveyCheckpointError("Survey checkpoint successor listing is invalid")
        candidates: list[SurveyCheckpoint] = []
        for item in contents:
            key = item.get("Key") if isinstance(item, dict) else None
            if not isinstance(key, str):
                continue
            match = re.fullmatch(
                re.escape(f"{prefix}/manifests/{expected_sequence:08d}-") + r"([0-9a-f]{64})\.json",
                key,
            )
            if match is None:
                continue
            manifest_sha256 = match.group(1)
            payload = self._get_bytes(key, maximum=_MANIFEST_MAX_BYTES)
            if hashlib.sha256(payload).hexdigest() != manifest_sha256:
                raise SurveyCheckpointError("Survey checkpoint successor hash is invalid")
            try:
                raw_manifest = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SurveyCheckpointError("Survey checkpoint successor JSON is invalid") from exc
            manifest, _files, units = _validate_manifest(
                raw_manifest,
                user_id=user_id,
                job_id=job_id,
                prefix=prefix,
            )
            if (
                manifest["sequence"] == expected_sequence
                and manifest["parent_manifest_sha256"] == parent_manifest_sha256
                and manifest["workflow_version"] == workflow_version
                and manifest["executor_version"] == executor_version
            ):
                candidates.append(
                    SurveyCheckpoint(
                        sequence=expected_sequence,
                        stage=manifest["stage"],
                        manifest_key=key,
                        manifest_sha256=manifest_sha256,
                        completed_units=units,
                        manifest=manifest,
                    )
                )
        if len(candidates) > 1:
            raise SurveyCheckpointError("Multiple Survey checkpoint successors are valid")
        return candidates[0] if candidates else None

    def _restore_sync(
        self,
        *,
        user_id: int,
        job_id: UUID,
        run_root: Path,
        manifest_key: str,
        manifest_sha256: str,
    ) -> SurveyCheckpoint:
        prefix = self.prefix(user_id=user_id, job_id=job_id)
        if (
            _SHA256.fullmatch(manifest_sha256) is None
            or not manifest_key.startswith(f"{prefix}/manifests/")
            or not manifest_key.endswith(f"-{manifest_sha256}.json")
        ):
            raise SurveyCheckpointError("Survey checkpoint manifest location is invalid")
        payload = self._get_bytes(manifest_key, maximum=_MANIFEST_MAX_BYTES)
        if hashlib.sha256(payload).hexdigest() != manifest_sha256:
            raise SurveyCheckpointError("Survey checkpoint manifest hash is invalid")
        try:
            raw_manifest = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SurveyCheckpointError("Survey checkpoint manifest JSON is invalid") from exc
        manifest, files, units = _validate_manifest(
            raw_manifest,
            user_id=user_id,
            job_id=job_id,
            prefix=prefix,
        )
        if run_root.exists() and any(run_root.iterdir()):
            raise SurveyCheckpointError("Survey checkpoint restore workspace is not empty")
        run_root.mkdir(parents=True, exist_ok=True)
        if run_root.is_symlink():
            raise SurveyCheckpointError("Survey checkpoint restore path is invalid")
        resolved_root = run_root.resolve(strict=True)
        for record in files:
            destination = resolved_root.joinpath(*PurePosixPath(record["path"]).parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.parent.resolve(strict=True) != destination.parent:
                raise SurveyCheckpointError("Survey checkpoint restore path is invalid")
            temp = destination.with_name(f".{destination.name}.{uuid4().hex}.checkpoint")
            digest = hashlib.sha256()
            size = 0
            try:
                response = self._client.get_object(
                    Bucket=self._bucket,
                    Key=record["object_key"],
                )
                body = response["Body"]
                try:
                    with temp.open("xb") as output:
                        while chunk := body.read(_READ_CHUNK_BYTES):
                            size += len(chunk)
                            if size > record["size"]:
                                raise SurveyCheckpointError(
                                    "Survey checkpoint restored file size is invalid"
                                )
                            digest.update(chunk)
                            output.write(chunk)
                finally:
                    close = getattr(body, "close", None)
                    if callable(close):
                        close()
                if size != record["size"] or digest.hexdigest() != record["sha256"]:
                    raise SurveyCheckpointError("Survey checkpoint restored file hash is invalid")
                os.replace(temp, destination)
            finally:
                temp.unlink(missing_ok=True)
        return SurveyCheckpoint(
            sequence=manifest["sequence"],
            stage=manifest["stage"],
            manifest_key=manifest_key,
            manifest_sha256=manifest_sha256,
            completed_units=units,
            manifest=manifest,
        )


def _is_precondition_failed(exc: ClientError) -> bool:
    return str(exc.response.get("Error", {}).get("Code")) in {"412", "PreconditionFailed"}


def _validate_manifest(
    value: object,
    *,
    user_id: int,
    job_id: UUID,
    prefix: str,
) -> tuple[dict[str, Any], list[dict[str, Any]], tuple[str, ...]]:
    if not isinstance(value, dict):
        raise SurveyCheckpointError("Survey checkpoint manifest is invalid")
    manifest = dict(value)
    parent = manifest.get("parent_manifest_sha256")
    base_valid = (
        manifest.get("schema_version") == 1
        and manifest.get("user_id") == user_id
        and manifest.get("job_id") == str(job_id)
        and isinstance(manifest.get("sequence"), int)
        and not isinstance(manifest.get("sequence"), bool)
        and manifest["sequence"] >= 1
        and isinstance(manifest.get("stage"), str)
        and _NAME.fullmatch(manifest["stage"]) is not None
        and isinstance(manifest.get("workflow_version"), str)
        and _NAME.fullmatch(manifest["workflow_version"]) is not None
        and isinstance(manifest.get("executor_version"), str)
        and _NAME.fullmatch(manifest["executor_version"]) is not None
        and (parent is None or (isinstance(parent, str) and _SHA256.fullmatch(parent) is not None))
    )
    if not base_valid:
        raise SurveyCheckpointError("Survey checkpoint manifest is invalid")
    raw_units = manifest.get("completed_units")
    if not isinstance(raw_units, list) or any(
        not isinstance(unit, str) or _NAME.fullmatch(unit) is None for unit in raw_units
    ):
        raise SurveyCheckpointError("Survey checkpoint completed units are invalid")
    units = tuple(sorted(set(raw_units)))
    if len(units) != len(raw_units):
        raise SurveyCheckpointError("Survey checkpoint completed units are duplicated")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise SurveyCheckpointError("Survey checkpoint files are invalid")
    files: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    total = 0
    for value_record in raw_files:
        if not isinstance(value_record, dict):
            raise SurveyCheckpointError("Survey checkpoint file is invalid")
        record = dict(value_record)
        path = record.get("path")
        size = record.get("size")
        sha256 = record.get("sha256")
        object_key = record.get("object_key")
        if (
            not isinstance(path, str)
            or not _durable_relative_path(path)
            or path in seen_paths
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or size > _FILE_MAX_BYTES
            or not isinstance(sha256, str)
            or _SHA256.fullmatch(sha256) is None
            or object_key != f"{prefix}/objects/{sha256}"
        ):
            raise SurveyCheckpointError("Survey checkpoint file path or metadata is invalid")
        total += size
        if total > _WORKSPACE_MAX_BYTES:
            raise SurveyCheckpointError("Survey checkpoint workspace exceeds the size limit")
        seen_paths.add(path)
        files.append(record)
    return manifest, files, units


__all__ = ["SurveyCheckpoint", "SurveyCheckpointError", "SurveyCheckpointStore"]
