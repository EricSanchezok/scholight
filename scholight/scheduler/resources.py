"""Bounded downloads and safe source extraction for one paper attempt."""

from __future__ import annotations

import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx

ARXIV_SOURCE_ORIGIN = "https://arxiv.org"
ARXIV_PDF_ORIGINS = ("https://arxiv.org", "https://export.arxiv.org")

_MAX_DOWNLOAD = 100 * 1024 * 1024
_MAX_MEMBERS = 10_000
_MAX_MEMBER = 50 * 1024 * 1024
_MAX_EXPANDED = 500 * 1024 * 1024


class ResourceUnavailableError(Exception):
    """All deterministic source locations returned not found."""


class ResourceTemporaryError(Exception):
    """The source could not be fetched due to a temporary condition."""


class ResourceCorruptError(Exception):
    """The fetched resource failed bounded validation."""


@dataclass(frozen=True, slots=True)
class DownloadedResource:
    kind: str
    path: Path


def _download(url: str, destination: Path) -> int:
    destination.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    try:
        with (
            httpx.Client(timeout=httpx.Timeout(120, connect=10), follow_redirects=True) as client,
            client.stream("GET", url) as response,
        ):
            status = response.status_code
            if status != 200:
                return status
            with destination.open("wb") as output:
                for block in response.iter_bytes(1024 * 1024):
                    size += len(block)
                    if size > _MAX_DOWNLOAD:
                        raise ResourceCorruptError("Resource exceeds download limit")
                    output.write(block)
    except (httpx.HTTPError, OSError) as exc:
        destination.unlink(missing_ok=True)
        raise ResourceTemporaryError("Resource download failed") from exc
    return 200


def _valid_pdf(path: Path) -> bool:
    if not path.exists() or path.stat().st_size < 512:
        return False
    with path.open("rb") as stream:
        return stream.read(5).startswith(b"%PDF-")


def _extract_source(archive_path: Path, destination: Path) -> None:
    staging = destination.with_name(f"{destination.name}.extracting")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    expanded = 0
    members = 0
    try:
        with tarfile.open(archive_path, mode="r:*") as archive:
            for member in archive:
                members += 1
                if members > _MAX_MEMBERS or member.size > _MAX_MEMBER:
                    raise ResourceCorruptError("Source archive exceeds extraction limits")
                target = (staging / member.name).resolve()
                if (
                    not target.is_relative_to(staging)
                    or member.issym()
                    or member.islnk()
                    or member.isdev()
                    or not (member.isdir() or member.isfile())
                ):
                    raise ResourceCorruptError("Source archive contains an unsafe member")
                expanded += member.size
                if expanded > _MAX_EXPANDED:
                    raise ResourceCorruptError("Source archive exceeds expanded size limit")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                source = archive.extractfile(member)
                if source is None:
                    raise ResourceCorruptError("Source archive contains an unreadable file")
                target.parent.mkdir(parents=True, exist_ok=True)
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
        shutil.rmtree(destination, ignore_errors=True)
        staging.replace(destination)
    except (OSError, tarfile.TarError, ResourceCorruptError) as exc:
        shutil.rmtree(staging, ignore_errors=True)
        if isinstance(exc, ResourceCorruptError):
            raise
        raise ResourceCorruptError("Source archive could not be extracted") from None


def fetch_paper_resource(arxiv_id: str, version: int, scratch: Path) -> DownloadedResource:
    """Fetch the exact arXiv revision, preferring source and falling back to PDF."""
    versioned_id = f"{arxiv_id}v{version}"
    source_archive = scratch / "source.tar"
    source_status = _download(f"{ARXIV_SOURCE_ORIGIN}/src/{versioned_id}", source_archive)
    if source_status == 200:
        source_dir = scratch / "source"
        _extract_source(source_archive, source_dir)
        source_archive.unlink(missing_ok=True)
        return DownloadedResource("latex", source_dir)
    source_archive.unlink(missing_ok=True)
    if source_status in {429, 500, 502, 503, 504}:
        raise ResourceTemporaryError("arXiv source is temporarily unavailable")

    pdf_path = scratch / "paper.pdf"
    statuses: list[int] = []
    for origin in ARXIV_PDF_ORIGINS:
        status = _download(f"{origin}/pdf/{versioned_id}.pdf", pdf_path)
        statuses.append(status)
        if status == 200 and _valid_pdf(pdf_path):
            return DownloadedResource("pdf", pdf_path)
        pdf_path.unlink(missing_ok=True)
        if status in {429, 500, 502, 503, 504}:
            raise ResourceTemporaryError("arXiv PDF is temporarily unavailable")
    if all(status == 404 for status in [source_status, *statuses]):
        raise ResourceUnavailableError("Exact arXiv revision was not found")
    raise ResourceCorruptError("Downloaded arXiv resources were invalid")
