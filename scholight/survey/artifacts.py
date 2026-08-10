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

    def _read_manifest_sync(self, manifest_key: str) -> dict[str, Any]:
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
        manifest = self._read_manifest_sync(manifest_key)
        records = _validated_records(manifest, manifest_key=manifest_key)
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
        manifest = self._read_manifest_sync(manifest_key)
        record = next(
            (
                candidate
                for candidate in _validated_records(manifest, manifest_key=manifest_key)
                if candidate["path"] == path
            ),
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
        manifest = self._read_manifest_sync(manifest_key)
        records = _validated_records(manifest, manifest_key=manifest_key)
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
        if storage_prefix != expected_prefix or manifest_key != f"{expected_prefix}/manifest.json":
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
]
