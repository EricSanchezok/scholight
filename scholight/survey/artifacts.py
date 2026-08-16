"""Private, owner-scoped Survey run archiving in S3."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import mimetypes
import os
import re
import stat
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import UUID, uuid4

import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import ClientError

logger = structlog.get_logger(__name__)

_READ_CHUNK_BYTES = 1024 * 1024
_MANIFEST_MAX_BYTES = 8 * 1024 * 1024
_RECOVERY_FILE_MAX_BYTES = 16 * 1024 * 1024
_RECOVERY_TOTAL_MAX_BYTES = 256 * 1024 * 1024
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_HTML_COMMENT_BYTES_PATTERN = re.compile(rb"<!--.*?-->", re.DOTALL)


def _s3_client(endpoint_url: str | None) -> Any:
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url,
        config=Config(
            connect_timeout=3,
            read_timeout=30,
            retries={"max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": "path" if endpoint_url else "auto"},
        ),
    )


class SurveyArtifactError(Exception):
    """A Survey run could not be safely archived or retrieved."""


class SurveyArtifactNotFoundError(SurveyArtifactError):
    """The requested path is not authorized by the archived manifest."""


@dataclass(frozen=True, slots=True)
class SurveyArchive:
    storage_prefix: str
    manifest_key: str
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SurveyRecoveryOverlay:
    source_manifest_key: str
    source_manifest_sha256: str
    storage_prefix: str
    manifest_key: str
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SurveyArtifactStream:
    path: str
    size: int
    sha256: str
    content_type: str
    _body: Any

    async def chunks(self) -> AsyncIterator[bytes]:
        """Read a private S3 object without materializing the complete file."""
        try:
            while chunk := await asyncio.to_thread(self._body.read, _READ_CHUNK_BYTES):
                yield chunk
        finally:
            close = getattr(self._body, "close", None)
            if callable(close):
                await asyncio.to_thread(close)


@dataclass(frozen=True, slots=True)
class _ManifestLocation:
    user_id: int
    job_id: UUID
    base_prefix: str
    recovery_sha256: str | None


@dataclass(frozen=True, slots=True)
class _ResolvedManifest:
    manifest: dict[str, Any]
    records: list[dict[str, Any]]
    deletion_records: list[dict[str, Any]]
    manifest_keys: tuple[str, ...]


def _sha256_stream(stream: Any) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(_READ_CHUNK_BYTES):
        digest.update(chunk)
    return digest.hexdigest()


def _content_type(path: Path) -> str:
    # Python's platform MIME database is not consistent for Markdown.  Keep the
    # public report contract deterministic across the development and runtime
    # images instead of trusting /etc/mime.types.
    if path.suffix.lower() in {".md", ".markdown"}:
        return "text/markdown"
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


def _manifest_location(manifest_key: str) -> _ManifestLocation:
    parts = manifest_key.split("/")
    is_v1 = len(parts) == 5 and parts[4] == "manifest.json"
    is_v2 = (
        len(parts) == 7
        and parts[4] == "recoveries"
        and _SHA256_PATTERN.fullmatch(parts[5]) is not None
        and parts[6] == "manifest.json"
    )
    if parts[:2] != ["surveys", "v1"] or not (is_v1 or is_v2):
        raise SurveyArtifactError("Survey artifact manifest key is invalid")
    try:
        user_id = int(parts[2])
        job_id = UUID(parts[3])
    except (TypeError, ValueError) as exc:
        raise SurveyArtifactError("Survey artifact manifest key is invalid") from exc
    return _ManifestLocation(
        user_id=user_id,
        job_id=job_id,
        base_prefix="/".join(parts[:4]),
        recovery_sha256=parts[5] if is_v2 else None,
    )


def _validate_manifest_owner(
    manifest: dict[str, Any],
    *,
    location: _ManifestLocation,
) -> None:
    if manifest.get("user_id") != location.user_id or manifest.get("job_id") != str(
        location.job_id
    ):
        raise SurveyArtifactError("Survey artifact manifest ownership is invalid")


def _validated_record_list(
    files: list[Any],
    *,
    object_prefix: str,
    manifest_key: str,
    allowed_paths: set[str] | None = None,
    error_label: str = "entry",
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_paths: set[str] = set()
    for record in files:
        if not isinstance(record, dict):
            raise SurveyArtifactError(f"Survey artifact manifest {error_label} is invalid")
        key = record.get("key")
        path = record.get("path")
        size = record.get("size")
        sha256 = record.get("sha256")
        mime = record.get("mime")
        path_parts = PurePosixPath(path).parts if isinstance(path, str) else ()
        expected_key = f"{object_prefix}/{path}" if isinstance(path, str) else None
        if (
            not isinstance(key, str)
            or key != expected_key
            or key == manifest_key
            or key in seen_keys
            or not isinstance(path, str)
            or not path
            or path in seen_paths
            or path.startswith("/")
            or ".." in path_parts
            or (path != "run.json" and (not path_parts or path_parts[0] != "run"))
            or (allowed_paths is not None and path not in allowed_paths)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(sha256, str)
            or _SHA256_PATTERN.fullmatch(sha256) is None
            or not isinstance(mime, str)
        ):
            raise SurveyArtifactError(f"Survey artifact manifest {error_label} is invalid")
        seen_keys.add(key)
        seen_paths.add(path)
        records.append(record)
    return records


def _validated_records(
    manifest: dict[str, Any],
    *,
    manifest_key: str,
) -> list[dict[str, Any]]:
    location = _manifest_location(manifest_key)
    if location.recovery_sha256 is not None or manifest.get("schema_version") != 1:
        raise SurveyArtifactError("Survey artifact manifest is invalid")
    _validate_manifest_owner(manifest, location=location)
    return _validated_record_list(
        manifest["files"],
        object_prefix=location.base_prefix,
        manifest_key=manifest_key,
    )


class SurveyArtifactStore:
    """Archive complete Survey workspaces without following unsafe filesystem entries."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        public_endpoint_url: str | None = None,
        client: Any | None = None,
        presign_client: Any | None = None,
    ) -> None:
        if not bucket.strip():
            raise ValueError("Survey artifact bucket is required")
        self._bucket = bucket
        self._client = client or _s3_client(endpoint_url)
        self._presign_client = presign_client or (
            _s3_client(public_endpoint_url)
            if public_endpoint_url and public_endpoint_url != endpoint_url
            else self._client
        )

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

    async def verify_access(self) -> None:
        """Exercise scoped Put/Get/Delete access without creating a Survey run."""
        key = f"surveys/v1/_smoke/{uuid4()}"
        body = os.urandom(32)
        await asyncio.to_thread(
            self._client.put_object,
            Bucket=self._bucket,
            Key=key,
            Body=body,
            ContentType="application/octet-stream",
        )
        try:
            await asyncio.to_thread(
                self._verify_object,
                key=key,
                expected_size=len(body),
                expected_sha256=hashlib.sha256(body).hexdigest(),
            )
        finally:
            await asyncio.to_thread(
                self._client.delete_object,
                Bucket=self._bucket,
                Key=key,
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
            self._verify_object(key=key, expected_size=size, expected_sha256=sha256)
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
        self._verify_object(
            key=run_key,
            expected_size=len(run_body),
            expected_sha256=hashlib.sha256(run_body).hexdigest(),
        )
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
        if len(manifest_body) > _MANIFEST_MAX_BYTES:
            raise SurveyArtifactError("Survey artifact manifest is too large")
        self._client.put_object(
            Bucket=self._bucket,
            Key=manifest_key,
            Body=manifest_body,
            ContentType="application/json",
        )
        self._verify_object(
            key=manifest_key,
            expected_size=len(manifest_body),
            expected_sha256=hashlib.sha256(manifest_body).hexdigest(),
        )
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

    def _verify_object(self, *, key: str, expected_size: int, expected_sha256: str) -> None:
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        body = response["Body"]
        try:
            size = int(response.get("ContentLength", expected_size))
            digest = _sha256_stream(body)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if size != expected_size or digest != expected_sha256:
            raise SurveyArtifactError("Survey artifact checksum verification failed")

    async def read_manifest(self, *, manifest_key: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._read_manifest_sync, manifest_key)

    async def read_manifest_with_sha256(
        self,
        *,
        manifest_key: str,
    ) -> tuple[dict[str, Any], str]:
        payload, raw = await asyncio.to_thread(
            self._read_manifest_document_sync,
            manifest_key,
        )
        return payload, hashlib.sha256(raw).hexdigest()

    async def validate_manifest(self, *, manifest_key: str) -> None:
        """Validate ownership, paths, and any v2 parent link without issuing URLs."""
        await asyncio.to_thread(self._resolve_manifest_sync, manifest_key)

    def _read_manifest_sync(self, manifest_key: str) -> dict[str, Any]:
        payload, _raw = self._read_manifest_document_sync(manifest_key)
        return payload

    def _read_manifest_document_sync(
        self,
        manifest_key: str,
    ) -> tuple[dict[str, Any], bytes]:
        response = self._client.get_object(Bucket=self._bucket, Key=manifest_key)
        body = response["Body"]
        try:
            raw = body.read(_MANIFEST_MAX_BYTES + 1)
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if len(raw) > _MANIFEST_MAX_BYTES:
            raise SurveyArtifactError("Survey artifact manifest is too large")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SurveyArtifactError("Survey artifact manifest is invalid") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") not in {1, 2}
            or not isinstance(payload.get("files"), list)
        ):
            raise SurveyArtifactError("Survey artifact manifest is invalid")
        return payload, raw

    def _resolve_manifest_sync(self, manifest_key: str) -> _ResolvedManifest:
        manifest, _raw = self._read_manifest_document_sync(manifest_key)
        location = _manifest_location(manifest_key)
        _validate_manifest_owner(manifest, location=location)
        if location.recovery_sha256 is None:
            records = _validated_records(manifest, manifest_key=manifest_key)
            return _ResolvedManifest(
                manifest=manifest,
                records=records,
                deletion_records=records,
                manifest_keys=(manifest_key,),
            )
        if manifest.get("schema_version") != 2:
            raise SurveyArtifactError("Survey artifact manifest is invalid")

        parent = manifest.get("parent_manifest")
        parent_key = parent.get("key") if isinstance(parent, dict) else None
        parent_sha256 = parent.get("sha256") if isinstance(parent, dict) else None
        expected_parent_key = f"{location.base_prefix}/manifest.json"
        if (
            parent_key != expected_parent_key
            or not isinstance(parent_sha256, str)
            or _SHA256_PATTERN.fullmatch(parent_sha256) is None
        ):
            raise SurveyArtifactError("Survey artifact parent manifest is invalid")
        parent_manifest, parent_raw = self._read_manifest_document_sync(parent_key)
        if hashlib.sha256(parent_raw).hexdigest() != parent_sha256:
            raise SurveyArtifactError("Survey artifact parent checksum is invalid")
        parent_location = _manifest_location(parent_key)
        if parent_manifest.get("schema_version") != 1:
            raise SurveyArtifactError("Survey artifact parent manifest is invalid")
        _validate_manifest_owner(parent_manifest, location=parent_location)
        parent_records = _validated_records(parent_manifest, manifest_key=parent_key)

        recovery_prefix = f"{location.base_prefix}/recoveries/{location.recovery_sha256}"
        overrides = _validated_record_list(
            manifest["files"],
            object_prefix=recovery_prefix,
            manifest_key=manifest_key,
            allowed_paths={"run/08_survey.md", "run/index.md"},
            error_label="overlay entry",
        )
        if {record["path"] for record in overrides} != {
            "run/08_survey.md",
            "run/index.md",
        }:
            raise SurveyArtifactError("Survey artifact manifest overlay entry is invalid")
        report = next(record for record in overrides if record["path"] == "run/08_survey.md")
        if report["sha256"] != location.recovery_sha256:
            raise SurveyArtifactError("Survey artifact manifest overlay entry is invalid")

        merged = {str(record["path"]): record for record in parent_records}
        for record in overrides:
            merged[str(record["path"])] = record
        return _ResolvedManifest(
            manifest=manifest,
            records=list(merged.values()),
            deletion_records=[*parent_records, *overrides],
            manifest_keys=(parent_key, manifest_key),
        )

    async def create_recovery_overlay(
        self,
        *,
        source_manifest_key: str,
        expected_source_sha256: str,
        report_path: Path,
        index_path: Path,
    ) -> SurveyRecoveryOverlay:
        """Append one verified report/index overlay without mutating its v1 source."""
        return await asyncio.to_thread(
            self._create_recovery_overlay_sync,
            source_manifest_key,
            expected_source_sha256,
            report_path,
            index_path,
            True,
        )

    async def plan_recovery_overlay(
        self,
        *,
        source_manifest_key: str,
        expected_source_sha256: str,
        report_path: Path,
        index_path: Path,
    ) -> SurveyRecoveryOverlay:
        """Build and validate the exact v2 overlay without writing any objects."""
        return await asyncio.to_thread(
            self._create_recovery_overlay_sync,
            source_manifest_key,
            expected_source_sha256,
            report_path,
            index_path,
            False,
        )

    def _create_recovery_overlay_sync(
        self,
        source_manifest_key: str,
        expected_source_sha256: str,
        report_path: Path,
        index_path: Path,
        write: bool,
    ) -> SurveyRecoveryOverlay:
        if _SHA256_PATTERN.fullmatch(expected_source_sha256) is None:
            raise SurveyArtifactError("Survey recovery source checksum is invalid")
        source_manifest, source_raw = self._read_manifest_document_sync(source_manifest_key)
        location = _manifest_location(source_manifest_key)
        if location.recovery_sha256 is not None:
            raise SurveyArtifactError("Survey recovery source manifest is invalid")
        _validated_records(source_manifest, manifest_key=source_manifest_key)
        source_sha256 = hashlib.sha256(source_raw).hexdigest()
        if source_sha256 != expected_source_sha256:
            raise SurveyArtifactError("Survey recovery source checksum changed")

        outputs = (
            ("run/08_survey.md", report_path, "08_survey.md"),
            ("run/index.md", index_path, "index.md"),
        )
        content_by_path: dict[str, bytes] = {}
        for relative_path, path, expected_name in outputs:
            try:
                path_stat = path.lstat()
            except OSError as exc:
                raise SurveyArtifactError("Survey recovery output is unavailable") from exc
            if (
                path.name != expected_name
                or not stat.S_ISREG(path_stat.st_mode)
                or path.is_symlink()
                or path_stat.st_size <= 0
                or path_stat.st_size > _RECOVERY_FILE_MAX_BYTES
            ):
                raise SurveyArtifactError("Survey recovery output is invalid")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(path, flags)
                with os.fdopen(descriptor, "rb") as handle:
                    content = handle.read(_RECOVERY_FILE_MAX_BYTES + 1)
            except OSError as exc:
                raise SurveyArtifactError("Survey recovery output is unreadable") from exc
            if len(content) != path_stat.st_size or len(content) > _RECOVERY_FILE_MAX_BYTES:
                raise SurveyArtifactError("Survey recovery output changed while reading")
            content_by_path[relative_path] = content

        report_sha256 = hashlib.sha256(content_by_path["run/08_survey.md"]).hexdigest()
        recovery_prefix = f"{location.base_prefix}/recoveries/{report_sha256}"
        manifest_key = f"{recovery_prefix}/manifest.json"
        records: list[dict[str, Any]] = []
        for relative_path, _path, _expected_name in outputs:
            content = content_by_path[relative_path]
            key = f"{recovery_prefix}/{relative_path}"
            if write:
                self._put_append_only(
                    key=key,
                    content=content,
                    content_type="text/markdown",
                )
            records.append(
                {
                    "path": relative_path,
                    "key": key,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "mime": "text/markdown",
                }
            )
        manifest: dict[str, Any] = {
            "schema_version": 2,
            "job_id": str(location.job_id),
            "user_id": location.user_id,
            "parent_manifest": {
                "key": source_manifest_key,
                "sha256": source_sha256,
            },
            "files": records,
        }
        manifest_body = json.dumps(
            manifest,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if write:
            self._put_append_only(
                key=manifest_key,
                content=manifest_body,
                content_type="application/json",
            )
        return SurveyRecoveryOverlay(
            source_manifest_key=source_manifest_key,
            source_manifest_sha256=source_sha256,
            storage_prefix=location.base_prefix,
            manifest_key=manifest_key,
            manifest=manifest,
        )

    def _put_append_only(self, *, key: str, content: bytes, content_type: str) -> None:
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
                IfNoneMatch="*",
            )
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"PreconditionFailed", "412", "ConditionalRequestConflict"}:
                raise
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
            try:
                existing = body.read(len(content) + 1)
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
            if existing != content:
                raise SurveyArtifactError("Survey recovery append-only object changed") from exc
            return
        self._verify_object(
            key=key,
            expected_size=len(content),
            expected_sha256=hashlib.sha256(content).hexdigest(),
        )

    async def presigned_artifacts(
        self,
        *,
        manifest_key: str,
        expires_seconds: int = 300,
    ) -> list[dict[str, Any]]:
        resolved = await asyncio.to_thread(self._resolve_manifest_sync, manifest_key)
        return await asyncio.to_thread(
            self._presigned_artifacts_sync,
            resolved.records,
            expires_seconds,
        )

    def _presigned_artifacts_sync(
        self,
        records: list[dict[str, Any]],
        expires_seconds: int,
    ) -> list[dict[str, Any]]:
        artifacts: list[dict[str, Any]] = []
        for record in records:
            artifacts.append(
                {
                    **record,
                    "url": self._presign_client.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": self._bucket, "Key": record["key"]},
                        ExpiresIn=expires_seconds,
                    ),
                }
            )
        return artifacts

    async def open_artifact(
        self,
        *,
        manifest_key: str,
        path: str,
    ) -> SurveyArtifactStream:
        """Open exactly one manifest-authorized object for bounded streaming."""
        return await asyncio.to_thread(self._open_artifact_sync, manifest_key, path)

    async def restore_contract_workspace(
        self,
        *,
        manifest_key: str,
        run_root: Path,
    ) -> dict[str, str]:
        """Restore only bounded contract inputs into a new private workspace."""
        return await asyncio.to_thread(
            self._restore_contract_workspace_sync,
            manifest_key,
            run_root,
        )

    def _restore_contract_workspace_sync(
        self,
        manifest_key: str,
        run_root: Path,
    ) -> dict[str, str]:
        records = self._resolve_manifest_sync(manifest_key).records
        selected: list[dict[str, Any]] = []
        total_size = 0
        image_suffixes = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
        for record in records:
            path = PurePosixPath(str(record["path"]))
            if not path.parts or path.parts[0] != "run":
                continue
            relative = PurePosixPath(*path.parts[1:])
            suffix = relative.suffix.casefold()
            is_contract_text = suffix in {".md", ".json"}
            is_contract_image = (
                len(relative.parts) == 1
                and relative.stem == "08_global_picture"
                and suffix in image_suffixes
            )
            if not (is_contract_text or is_contract_image):
                continue
            size = int(record["size"])
            if size > _RECOVERY_FILE_MAX_BYTES:
                raise SurveyArtifactError("Survey recovery artifact exceeds the file limit")
            total_size += size
            if total_size > _RECOVERY_TOTAL_MAX_BYTES:
                raise SurveyArtifactError("Survey recovery workspace exceeds the total limit")
            selected.append(record)

        try:
            run_root.mkdir(parents=True, exist_ok=False)
            resolved_root = run_root.resolve(strict=True)
        except OSError as exc:
            raise SurveyArtifactError("Survey recovery workspace could not be created") from exc

        restored: dict[str, str] = {}
        for record in selected:
            relative = PurePosixPath(str(record["path"])).relative_to("run")
            target = resolved_root.joinpath(*relative.parts)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.parent.resolve(strict=True) != resolved_root.joinpath(
                    *relative.parts[:-1]
                ).resolve(strict=True):
                    raise SurveyArtifactError("Survey recovery artifact path is unsafe")
                content = self._read_record_bytes(record)
                descriptor = os.open(
                    target,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            except SurveyArtifactError:
                raise
            except OSError as exc:
                raise SurveyArtifactError("Survey recovery artifact could not be written") from exc
            restored[relative.as_posix()] = str(record["sha256"])
        return restored

    def _open_artifact_sync(self, manifest_key: str, path: str) -> SurveyArtifactStream:
        records = self._resolve_manifest_sync(manifest_key).records
        record = next(
            (candidate for candidate in records if candidate["path"] == path),
            None,
        )
        if record is None:
            raise SurveyArtifactNotFoundError("Survey artifact is not present in the manifest")
        response = self._client.get_object(Bucket=self._bucket, Key=record["key"])
        body = response["Body"]
        if int(response.get("ContentLength", -1)) != int(record["size"]):
            close = getattr(body, "close", None)
            if callable(close):
                close()
            raise SurveyArtifactError("Survey artifact size does not match the manifest")
        return SurveyArtifactStream(
            path=str(record["path"]),
            size=int(record["size"]),
            sha256=str(record["sha256"]),
            content_type=str(record["mime"]),
            _body=body,
        )

    async def build_report_package(self, *, manifest_key: str) -> SurveyArtifactStream:
        """Build a portable ZIP containing the final Markdown report and its images."""
        return await asyncio.to_thread(self._build_report_package_sync, manifest_key)

    def _build_report_package_sync(self, manifest_key: str) -> SurveyArtifactStream:
        records = self._resolve_manifest_sync(manifest_key).records
        report = next(
            (record for record in records if record["path"] == "run/08_survey.md"),
            None,
        )
        if report is None:
            raise SurveyArtifactNotFoundError("Survey report is not present in the manifest")
        images = sorted(
            (
                record
                for record in records
                if str(record["path"]).startswith("run/")
                and str(record["mime"]).startswith("image/")
            ),
            key=lambda record: str(record["path"]),
        )

        package = io.BytesIO()
        package_records: list[dict[str, Any]] = []
        with zipfile.ZipFile(package, mode="w", compression=zipfile.ZIP_DEFLATED) as report_zip:
            for record in [report, *images]:
                content = self._read_record_bytes(record)
                if record is report:
                    content = _HTML_COMMENT_BYTES_PATTERN.sub(b"", content)
                archive_path = PurePosixPath(str(record["path"])).relative_to("run").as_posix()
                report_zip.writestr(archive_path, content)
                package_records.append(
                    {
                        "path": archive_path,
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "mime": str(record["mime"]),
                    }
                )
            package_manifest = json.dumps(
                {"schema_version": 1, "files": package_records},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            report_zip.writestr("manifest.json", package_manifest)

        size = package.tell()
        package.seek(0)
        sha256 = _sha256_stream(package)
        package.seek(0)
        return SurveyArtifactStream(
            path="scholight-survey.zip",
            size=size,
            sha256=sha256,
            content_type="application/zip",
            _body=package,
        )

    def _read_record_bytes(self, record: dict[str, Any]) -> bytes:
        response = self._client.get_object(Bucket=self._bucket, Key=record["key"])
        body = response["Body"]
        expected_size = int(record["size"])
        digest = hashlib.sha256()
        content = io.BytesIO()
        try:
            declared_size = int(response.get("ContentLength", -1))
            while chunk := body.read(_READ_CHUNK_BYTES):
                digest.update(chunk)
                content.write(chunk)
                if content.tell() > expected_size:
                    raise SurveyArtifactError("Survey artifact does not match the manifest")
        finally:
            close = getattr(body, "close", None)
            if callable(close):
                close()
        if (
            declared_size != expected_size
            or content.tell() != expected_size
            or digest.hexdigest() != record["sha256"]
        ):
            raise SurveyArtifactError("Survey artifact does not match the manifest")
        return content.getvalue()

    async def delete_archive(self, *, manifest_key: str, preserve_manifest: bool = False) -> None:
        resolved = await asyncio.to_thread(self._resolve_manifest_sync, manifest_key)
        await asyncio.to_thread(
            self._delete_archive_sync,
            resolved,
            preserve_manifest,
        )

    def _delete_archive_sync(
        self,
        resolved: _ResolvedManifest,
        preserve_manifest: bool,
    ) -> None:
        keys = list({record["key"] for record in resolved.deletion_records})
        if not preserve_manifest:
            keys.extend(resolved.manifest_keys)
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
        resolved = await asyncio.to_thread(self._resolve_manifest_sync, manifest_key)
        for key in resolved.manifest_keys:
            await asyncio.to_thread(
                self._client.delete_object,
                Bucket=self._bucket,
                Key=key,
            )

    async def cleanup_archive(
        self,
        *,
        user_id: int,
        job_id: UUID,
        storage_prefix: str,
        manifest_key: str,
    ) -> None:
        """Delete exact manifest objects, or the server-generated job prefix if absent."""
        expected_prefix = self.prefix(user_id=user_id, job_id=job_id)
        try:
            location = _manifest_location(manifest_key)
        except SurveyArtifactError as exc:
            raise SurveyArtifactError("Survey artifact cleanup scope is invalid") from exc
        if (
            storage_prefix != expected_prefix
            or location.base_prefix != expected_prefix
            or location.user_id != user_id
            or location.job_id != job_id
        ):
            raise SurveyArtifactError("Survey artifact cleanup scope is invalid")
        try:
            await self.delete_archive(manifest_key=manifest_key)
            return
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code not in {"NoSuchKey", "404", "NotFound"}:
                raise
        await asyncio.to_thread(self._delete_prefix_sync, expected_prefix)

    def _delete_prefix_sync(self, prefix: str) -> None:
        continuation: str | None = None
        while True:
            parameters: dict[str, Any] = {
                "Bucket": self._bucket,
                "Prefix": f"{prefix}/",
                "MaxKeys": 1000,
            }
            if continuation is not None:
                parameters["ContinuationToken"] = continuation
            response = self._client.list_objects_v2(**parameters)
            keys = [
                item["Key"]
                for item in response.get("Contents", [])
                if isinstance(item, dict)
                and isinstance(item.get("Key"), str)
                and item["Key"].startswith(f"{prefix}/")
            ]
            if keys:
                deleted = self._client.delete_objects(
                    Bucket=self._bucket,
                    Delete={"Objects": [{"Key": key} for key in keys], "Quiet": True},
                )
                if deleted.get("Errors"):
                    raise SurveyArtifactError("Survey artifact cleanup was incomplete")
            if not response.get("IsTruncated"):
                return
            continuation = response.get("NextContinuationToken")
            if not isinstance(continuation, str):
                raise SurveyArtifactError("Survey artifact cleanup pagination was invalid")


__all__ = [
    "SurveyArchive",
    "SurveyArtifactError",
    "SurveyArtifactNotFoundError",
    "SurveyArtifactStream",
    "SurveyArtifactStore",
    "SurveyRecoveryOverlay",
]
