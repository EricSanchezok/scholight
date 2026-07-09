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

        if proc.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
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


def _extract_tarball(tar_path: Path, dest_dir: Path) -> bool:
    """Extract tar.gz to *dest_dir*.  Returns ``True`` on success."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            tf.extractall(path=dest_dir)
        return True
    except (tarfile.ReadError, OSError):
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

            if aid in done or aid in failed_set:
                result.skipped += 1
                continue

            if not created:
                if log:
                    log.warning("paper missing created date, skipping", arxiv_id=aid)
                result.skipped += 1
                continue

            try:
                status = self._download_one(aid, created)

                if status == "pdf":
                    update_arxiv_paper(aid, {"has_pdf": True})
                    self._save_checkpoint(aid)
                    result.processed += 1
                elif status == "latex":
                    update_arxiv_paper(aid, {"has_latex": True})
                    self._save_checkpoint(aid)
                    result.processed += 1
                elif status == "permanent_failure":
                    self._failed_checkpoint(aid)
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
            output_fields=["arxiv_id", "created"],
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
