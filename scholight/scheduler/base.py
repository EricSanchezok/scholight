"""Base daemon infrastructure for long-running scheduler tasks.

Every daemon follows the same lifecycle:
1. on_startup()  — one-time init
2. process_batch() → fetch work, process each item independently, return stats
3. If work was found → short re-check (10 s)
4. If idle → full sleep_interval
5. on_shutdown() — cleanup
6. Repeat until SIGTERM/SIGINT
"""

from __future__ import annotations

import os
import signal
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import structlog

from scholight.logging import configure_logging
from scholight.storage import storage
from scholight.store.client import get_client


@dataclass
class BatchResult:
    """Outcome of a single process_batch() call."""

    processed: int = 0
    failed: int = 0
    skipped: int = 0  # already in checkpoint


class BaseDaemon(ABC):
    """Abstract base for long-running scheduler daemons.

    Subclasses define the work via :meth:`process_batch` and the daemon
    handles checkpoint persistence, graceful shutdown, periodic status
    reporting, and the inner poll/sleep loop.

    Attributes:
        name: Daemon identifier — used for log filenames and checkpoint dirs.
        sleep_interval: Seconds between polls when idle (fully caught up).
        batch_size: Maximum items per ``process_batch()`` call.
        recheck_interval: Seconds between polls when work was just found.
    """

    name: str
    sleep_interval: int
    batch_size: int
    recheck_interval: int = 10

    def __init__(self) -> None:
        if not self.name:
            raise ValueError("BaseDaemon subclass must set 'name'")
        if self.sleep_interval <= 0:
            raise ValueError("sleep_interval must be positive")
        if self.batch_size <= 0:
            raise ValueError("batch_size must be positive")

        self._shutdown = threading.Event()
        self._log: structlog.BoundLogger | None = None

    # ── Main loop ───────────────────────────────────────────────────

    def run(self) -> None:
        """Configure logging, call on_startup, enter process/report/sleep loop."""
        self._init_logging()
        self.on_startup()
        self._install_signal_handlers()

        logger = self._log
        assert logger is not None

        logger.info("daemon started", name=self.name, sleep_interval=self.sleep_interval)

        while not self._shutdown.is_set():
            try:
                result = self.process_batch()
            except Exception:
                logger.exception("process_batch crashed — sleeping before retry")
                self._shutdown.wait(self.recheck_interval)
                continue

            if result.processed > 0 or result.failed > 0:
                self._log_status(result)
                self._shutdown.wait(self.recheck_interval)
            else:
                self._shutdown.wait(self.sleep_interval)

        logger.info("daemon shutting down")
        self.on_shutdown()
        logger.info("daemon stopped")

    # ── Subclass MUST override ──────────────────────────────────────

    @abstractmethod
    def process_batch(self) -> BatchResult:
        """Fetch one batch of work, process each item, return stats.

        Each item MUST be processed independently — a failure on item X
        must NOT block item Y.  Use try/except per item, not per batch.

        Recommended pattern::

            def process_batch(self) -> BatchResult:
                items = self._fetch_work()
                result = BatchResult()
                done = self._load_checkpoint()
                failed_set = self._load_failed_checkpoint()
                for item in items:
                    aid = item["arxiv_id"]
                    if aid in done or aid in failed_set:
                        result.skipped += 1
                        continue
                    try:
                        ok = self._process_one(item)
                        if ok:
                            self._save_checkpoint(aid)
                            result.processed += 1
                        else:
                            self._failed_checkpoint(aid)
                            result.failed += 1
                    except Exception:
                        self._log.exception("item failed, deferring", arxiv_id=aid)
                        result.failed += 1
                        # DO NOT write checkpoint — will retry next poll
                return result
        """

    # ── Subclass MAY override ───────────────────────────────────────

    def on_startup(self) -> None:
        """One-time initialisation before the poll loop starts.

        Override to initialise connections, load models, warm caches, etc.
        """
        return

    def on_shutdown(self) -> None:
        """Cleanup before the daemon exits.

        Override to close connections, flush buffers, etc.
        """
        return

    # ── Checkpoint (provided by base) ───────────────────────────────

    def _checkpoint_dir(self) -> Path:
        """Return the checkpoint directory for this daemon."""
        return storage.checkpoint_dir(self.name)

    def _load_checkpoint(self) -> set[str]:
        """Return the set of already-processed checkpoint keys."""
        p = self._checkpoint_dir() / "done.txt"
        return self._read_ids(p)

    def _save_checkpoint(
        self, arxiv_id: str, version: object | None = None, updated: str | None = None
    ) -> None:
        """Mark one paper version as successfully processed (append + fsync)."""
        key = self._checkpoint_key(arxiv_id, version, updated)
        self._append_id(self._checkpoint_dir() / "done.txt", key)

    def _failed_checkpoint(
        self, arxiv_id: str, version: object | None = None, updated: str | None = None
    ) -> None:
        """Mark one paper version as permanently failed (append + fsync)."""
        key = self._checkpoint_key(arxiv_id, version, updated)
        self._append_id(self._checkpoint_dir() / "failed.txt", key)

    def _load_failed_checkpoint(self) -> set[str]:
        """Return the set of permanently failed checkpoint keys."""
        p = self._checkpoint_dir() / "failed.txt"
        return self._read_ids(p)

    @staticmethod
    def _checkpoint_key(
        arxiv_id: str, version: object | None = None, updated: str | None = None
    ) -> str:
        parts = [arxiv_id]
        if version not in (None, ""):
            parts.append(f"version={version}")
        if updated:
            parts.append(f"updated={updated}")
        return "\t".join(parts)

    @classmethod
    def _is_checkpointed(
        cls,
        entries: set[str],
        arxiv_id: str,
        version: object | None = None,
        updated: str | None = None,
    ) -> bool:
        return cls._checkpoint_key(arxiv_id, version, updated) in entries

    @staticmethod
    def _generation_is_current(arxiv_id: str, version: object | None, updated: str | None) -> bool:
        """Strongly verify that fetched work still belongs to the current paper generation."""
        rows = get_client().get(
            "arxiv_papers",
            ids=[arxiv_id],
            output_fields=["arxiv_id", "version", "updated"],
            consistency_level="Strong",
        )
        if not rows:
            return False
        current = rows[0]
        return current.get("version") == version and str(current.get("updated", "")) == str(
            updated or ""
        )

    # ── Status reporting (provided by base) ─────────────────────────

    def _log_status(self, result: BatchResult) -> None:
        """Log a one-line status update after each productive batch."""
        assert self._log is not None
        self._log.info(
            "batch complete",
            processed=result.processed,
            failed=result.failed,
            skipped=result.skipped,
        )

    # ── Internal helpers ────────────────────────────────────────────

    @staticmethod
    def _read_ids(path: Path) -> set[str]:
        if not path.exists():
            return set()
        return {line.strip() for line in path.read_text("utf-8").splitlines() if line.strip()}

    @staticmethod
    def _append_id(path: Path, arxiv_id: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{arxiv_id}\n")
            f.flush()
            os.fsync(f.fileno())

    def _init_logging(self) -> None:
        log_path = storage.log_path(self.name, "daemon.log")
        configure_logging(
            log_level="INFO",
            use_json=True,
            file_handler=(str(log_path), 50_000_000, 5),
        )
        self._log = structlog.get_logger(self.name)

    def _install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum: int, _frame: object) -> None:
        sig_name = signal.Signals(signum).name
        if self._log is not None:
            self._log.info("signal received, initiating shutdown", signal=sig_name)
        self._shutdown.set()
