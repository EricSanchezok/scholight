"""Stage B daemon — downloads PDFs for new papers with 3-tier fallback.

Design
------
1. Query Milvus for papers where ``has_pdf == false and has_latex == false``.
2. For each paper, run a 3-tier download with per-paper try/except isolation:
   - Tier 1: ``https://arxiv.org/pdf/{aid}.pdf``
   - Tier 2: ``https://export.arxiv.org/pdf/{aid}.pdf``
   - Tier 3: ``https://arxiv.org/src/{aid}`` (LaTeX source tar.gz)
3. Verify PDFs via ``_pdf_ok()`` (``%PDF`` header + min 512 B).
4. On success, update ``has_pdf`` / ``has_latex`` in Milvus and write checkpoint.
5. Transient failures (rate-limit, timeout, empty file) → no checkpoint → retry on next poll.
6. Permanent failures (404 on all three tiers) → ``failed.txt`` checkpoint → never retried.
"""

from __future__ import annotations

import gzip
import io
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Any

from scholight.scheduler.base import BaseDaemon, BatchResult
from scholight.storage import storage
from scholight.store.client import get_client
from scholight.store.ingest import update_arxiv_paper

# ── URL templates ──────────────────────────────────────────────────────

_ARXIV_PDF = "https://arxiv.org/pdf/{aid}.pdf"
_EXPORT_PDF = "https://export.arxiv.org/pdf/{aid}.pdf"
_ARXIV_SRC = "https://arxiv.org/src/{aid}"

# ── Rate limits (seconds between requests) ──────────────────────────────

_TIER1_DELAY = 3.0  # arxiv.org
_TIER2_DELAY = 1.0  # export.arxiv.org

# ── Curl timeouts ───────────────────────────────────────────────────────

_CONNECT_TIMEOUT = 10
_MAX_TIME = 120
_MAX_SOURCE_ARCHIVE_BYTES = 100 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_ARCHIVE_MEMBER_BYTES = 50 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 500 * 1024 * 1024
# Includes tar headers, PAX/GNU metadata records, padding, and file payloads.
_MAX_ARCHIVE_STREAM_BYTES = 600 * 1024 * 1024
_EXTRACT_CHUNK_BYTES = 1024 * 1024

# ── PDF validation ──────────────────────────────────────────────────────


def _pdf_ok(path: Path) -> bool:
    """Check for valid PDF header and minimum file size."""
    if not path.exists() or path.stat().st_size < 512:
        return False
    try:
        with open(path, "rb") as fh:
            return fh.read(5).startswith(b"%PDF")
    except OSError:
        return False


# ── Download helpers ────────────────────────────────────────────────────


def _curl_download(url: str, dest: Path) -> str | None:
    """Download a PDF via curl.  Returns ``None`` on success, error code string on failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                "curl",
                "-sSL",
                "-o",
                str(dest),
                "--connect-timeout",
                str(_CONNECT_TIMEOUT),
                "--max-time",
                str(_MAX_TIME),
                "-w",
                "%{http_code}",
                url,
            ],
            capture_output=True,
            text=False,
            timeout=_MAX_TIME + 10,
        )
        code_b = proc.stdout.strip()
        code = code_b.decode()

        if proc.returncode == 0 and _pdf_ok(dest):
            return None

        if dest.exists():
            dest.unlink(missing_ok=True)

        if code == "404":
            return "404"
        if code in ("429", "503"):
            return "rate_limit"
        if code and code != "200":
            return f"http_{code}"
        if proc.returncode != 0:
            return f"curl_{proc.returncode}"
        return "invalid_pdf"

    except subprocess.TimeoutExpired:
        if dest.exists():
            dest.unlink(missing_ok=True)
        return "timeout"


def _curl_download_src(url: str, dest: Path) -> str | None:
    """Download a source tar.gz via curl.  Returns ``None`` on success, error string on failure."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            [
                "curl",
                "-sSL",
                "-o",
                str(dest),
                "--connect-timeout",
                str(_CONNECT_TIMEOUT),
                "--max-time",
                str(_MAX_TIME),
                "--max-filesize",
                str(_MAX_SOURCE_ARCHIVE_BYTES),
                "-w",
                "%{http_code}",
                url,
            ],
            capture_output=True,
            text=False,
            timeout=_MAX_TIME + 10,
        )
        code_b = proc.stdout.strip()
        code = code_b.decode()

        if (
            proc.returncode == 0
            and dest.exists()
            and 0 < dest.stat().st_size <= _MAX_SOURCE_ARCHIVE_BYTES
        ):
            return None

        if dest.exists():
            dest.unlink(missing_ok=True)

        if code == "404":
            return "404"
        if code in ("429", "503"):
            return "rate_limit"
        if code and code != "200":
            return f"http_{code}"
        if proc.returncode != 0:
            return f"curl_{proc.returncode}"
        return "empty_file"

    except subprocess.TimeoutExpired:
        if dest.exists():
            dest.unlink(missing_ok=True)
        return "timeout"


class _LimitedArchiveStream(io.RawIOBase):
    """Bound all decompressed tar bytes, including parser-only metadata records."""

    def __init__(self, stream: gzip.GzipFile, limit: int) -> None:
        super().__init__()
        self._stream = stream
        self._remaining = limit

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = _EXTRACT_CHUNK_BYTES
        size = min(size, _EXTRACT_CHUNK_BYTES, self._remaining + 1)
        chunk = self._stream.read(size)
        self._remaining -= len(chunk)
        if self._remaining < 0:
            raise ValueError("archive decompressed stream limit exceeded")
        return chunk


def _extract_tarball(tar_path: Path, dest_dir: Path) -> bool:
    """Extract an arXiv source archive within strict path and resource limits."""
    dest_root = dest_dir.resolve()
    staging = dest_root.with_name(f"{dest_root.name}.extracting")
    shutil.rmtree(staging, ignore_errors=True)
    member_count = 0
    total_written = 0

    try:
        staging.mkdir(parents=True)
        with (
            gzip.open(tar_path, "rb") as compressed,
            tarfile.open(
                fileobj=_LimitedArchiveStream(compressed, _MAX_ARCHIVE_STREAM_BYTES),
                mode="r|",
            ) as archive,
        ):
            for member in archive:
                member_count += 1
                if member_count > _MAX_ARCHIVE_MEMBERS:
                    raise ValueError("archive member limit exceeded")

                target = (staging / member.name).resolve()
                if (
                    not target.is_relative_to(staging)
                    or member.issym()
                    or member.islnk()
                    or member.isdev()
                    or not (member.isdir() or member.isfile())
                ):
                    raise ValueError("unsafe archive member")
                if member.size > _MAX_ARCHIVE_MEMBER_BYTES:
                    raise ValueError("archive member size limit exceeded")
                if total_written + member.size > _MAX_ARCHIVE_TOTAL_BYTES:
                    raise ValueError("archive total size limit exceeded")

                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                source = archive.extractfile(member)
                if source is None:
                    raise ValueError("archive member is unreadable")
                target.parent.mkdir(parents=True, exist_ok=True)
                member_written = 0
                with source, target.open("wb") as output:
                    while chunk := source.read(_EXTRACT_CHUNK_BYTES):
                        member_written += len(chunk)
                        total_written += len(chunk)
                        if (
                            member_written > _MAX_ARCHIVE_MEMBER_BYTES
                            or total_written > _MAX_ARCHIVE_TOTAL_BYTES
                        ):
                            raise ValueError("archive expanded size limit exceeded")
                        output.write(chunk)
                if member_written != member.size:
                    raise ValueError("archive member size mismatch")

        shutil.rmtree(dest_root, ignore_errors=True)
        staging.replace(dest_root)
        return True
    except (tarfile.ReadError, OSError, ValueError):
        shutil.rmtree(staging, ignore_errors=True)
        return False


# ── Daemon ──────────────────────────────────────────────────────────────


class PdfDownloadDaemon(BaseDaemon):
    """Downloads PDFs for papers missing both PDF and LaTeX sources.

    3-tier fallback with per-paper isolation and rate limiting:

    - Tier 1: ``https://arxiv.org/pdf/{aid}.pdf`` — 3 s between requests
    - Tier 2: ``https://export.arxiv.org/pdf/{aid}.pdf`` — 1 s between requests
    - Tier 3: ``https://arxiv.org/src/{aid}`` — LaTeX source tar.gz

    Transient failures (rate-limit, timeout, empty files) are NOT checkpointed
    and will be retried on the **next poll**.  Papers that return 404 on **all
    three tiers** are written to ``failed.txt`` and never retried.
    """

    name = "pdf_download"
    sleep_interval = 300
    batch_size = 500

    def process_batch(self) -> BatchResult:
        papers = self._fetch_work()
        result = BatchResult()
        done = self._load_checkpoint()
        failed_set = self._load_failed_checkpoint()
        log = self._log

        for paper in papers:
            aid = paper["arxiv_id"]
            created = paper.get("created", "")
            version = paper.get("version")
            updated = paper.get("updated", "")

            if self._is_checkpointed(done, aid, version, updated) or self._is_checkpointed(
                failed_set, aid, version, updated
            ):
                result.skipped += 1
                continue

            if not created:
                if log:
                    log.warning("paper missing created date, skipping", arxiv_id=aid)
                result.skipped += 1
                continue

            try:
                with storage.generation_lock(aid):
                    if not self._generation_is_current(aid, version, updated):
                        result.skipped += 1
                        continue
                    status = self._download_one(aid, created)

                    if status == "pdf":
                        update_arxiv_paper(aid, {"has_pdf": True})
                        self._save_checkpoint(aid, version, updated)
                        result.processed += 1
                    elif status == "latex":
                        update_arxiv_paper(aid, {"has_latex": True})
                        self._save_checkpoint(aid, version, updated)
                        result.processed += 1
                    elif status == "permanent_failure":
                        self._failed_checkpoint(aid, version, updated)
                        result.failed += 1
                    else:  # "transient"
                        result.failed += 1
                        # No checkpoint — will retry next poll

            except Exception:
                if log:
                    log.exception("download failed, deferring", arxiv_id=aid)
                result.failed += 1
                # No checkpoint — will retry next poll

        return result

    # ── Internal ────────────────────────────────────────────────────

    @staticmethod
    def _fetch_work() -> list[dict[str, Any]]:
        """Query Milvus for papers missing both PDF and LaTeX."""
        client = get_client()
        rows = client.query(
            "arxiv_papers",
            filter="has_pdf == false and has_latex == false",
            output_fields=["arxiv_id", "created", "updated", "version"],
            limit=PdfDownloadDaemon.batch_size,
            timeout=30,
        )
        return list(rows)

    @staticmethod
    def _download_one(aid: str, created: str) -> str:
        """Download one paper with 3-tier fallback.

        Returns:
            ``"pdf"`` — PDF downloaded successfully.
            ``"latex"`` — LaTeX source downloaded and extracted.
            ``"permanent_failure"`` — 404 on all three tiers.
            ``"transient"`` — temporary failure (rate-limit, timeout, etc.).
        """
        pdf_dest = storage.pdf_path(aid, created)

        # Already on disk?
        if _pdf_ok(pdf_dest):
            return "pdf"

        # ── Tier 1: arxiv.org PDF ──
        err = _curl_download(_ARXIV_PDF.format(aid=aid), pdf_dest)
        time.sleep(_TIER1_DELAY)
        if err is None:
            return "pdf"
        if err == "404":
            pass  # fall through to Tier 2
        elif err in ("rate_limit", "timeout"):
            return "transient"
        # Other errors (invalid_pdf, http_xxx, curl_xxx): fall through

        # ── Tier 2: export.arxiv.org PDF ──
        err = _curl_download(_EXPORT_PDF.format(aid=aid), pdf_dest)
        time.sleep(_TIER2_DELAY)
        if err is None:
            return "pdf"
        if err == "404":
            pass  # fall through to Tier 3
        elif err in ("rate_limit", "timeout"):
            return "transient"

        # ── Tier 3: arxiv.org/src LaTeX source ──
        src_tmp = pdf_dest.with_suffix(".tar.gz")
        err = _curl_download_src(_ARXIV_SRC.format(aid=aid), src_tmp)
        if err is None:
            latex_dir = storage.latex_dir(aid, created)
            ok = _extract_tarball(src_tmp, latex_dir)
            src_tmp.unlink(missing_ok=True)
            return "latex" if ok else "transient"

        if err == "404":
            return "permanent_failure"

        return "transient"
