"""Paper resource, checkpoint, backup, and log directory management.

Every paper gets a deterministic directory based on its ``created`` date.
All derived files live together under ``{data_root}/papers/YYYY/MM/DD/{arxiv_id_safe}/``.

Checkpoints (BM25, etc.) live under ``{data_root}/checkpoints/``.
Backups (logical exports + file-level snapshots) live under ``{data_root}/backups/``.

Old-format arxiv_ids like ``"astro-ph/9608163"`` have ``/`` replaced with ``_``
in filesystem paths, keeping Milvus IDs unchanged.
"""

from __future__ import annotations

import fcntl
import hashlib
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from scholight.config import settings


class Storage:
    """Unified directory layout for paper resources, checkpoints, backups, and logs.

    Usage::

        from scholight.storage import storage

        pdf = storage.pdf_path("2505.12345", "2025-05-28")
        latex_dir = storage.latex_dir("astro-ph/9608163", "1996-08-15")
        ckpt = storage.checkpoint_path("bm25", "arxiv_abstract.pkl")
        backup = storage.backup_dir("logical")

        d = storage.paper_dir("2505.12345", "2025-05-28")
        pdf.write_bytes(data)
    """

    def __init__(self) -> None:
        self._root = Path(settings.data_root)
        self._papers_root = self._root / "papers"
        self._checkpoints_root = self._root / "checkpoints"
        self._backups_root = self._root / "backups"
        self._generation_locks_root = self._root / "locks" / "paper-generations"

    @property
    def root(self) -> Path:
        return self._root

    # ── Path computation (pure, zero I/O) ─────────────────────────────

    def _paper_dir(self, arxiv_id: str, created: str) -> Path:
        y, m, d = created.split("-", 2)
        return self._papers_root / y / m / d / arxiv_id.replace("/", "_")

    def pdf_path(self, arxiv_id: str, created: str) -> Path:
        return self._paper_dir(arxiv_id, created) / "paper.pdf"

    def latex_dir(self, arxiv_id: str, created: str) -> Path:
        """LaTeX source directory — extracted project with ``.tex``, ``.bbl``, images, etc."""
        return self._paper_dir(arxiv_id, created) / "latex"

    def markdown_path(self, arxiv_id: str, created: str) -> Path:
        return self._paper_dir(arxiv_id, created) / "paper.md"

    # ── Directory creation ────────────────────────────────────────────

    def paper_dir(self, arxiv_id: str, created: str) -> Path:
        """Create and return the paper resource directory."""
        p = self._paper_dir(arxiv_id, created)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def remove_paper_dir(self, arxiv_id: str, created: str) -> None:
        """Remove all local resources owned by a paper."""
        paper_dir = self._paper_dir(arxiv_id, created)
        if paper_dir.exists():
            shutil.rmtree(paper_dir)

    @contextmanager
    def generation_lock(self, arxiv_id: str) -> Iterator[None]:
        """Serialize one paper generation across scheduler containers.

        The lock file lives on the shared data volume, so paper sync and all
        derived-artifact workers use the same cross-process critical section.
        """
        digest = hashlib.sha256(arxiv_id.encode("utf-8")).hexdigest()
        self._generation_locks_root.mkdir(parents=True, exist_ok=True)
        lock_path = self._generation_locks_root / f"{digest}.lock"
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def ensure_log(self, subdir: str) -> Path:
        _validate_path_component(subdir, label="log subdir")
        d = self._root / "logs" / subdir
        d.mkdir(parents=True, exist_ok=True)
        return d

    def log_path(self, subdir: str, filename: str = "app.log") -> Path:
        _validate_path_component(subdir, label="log subdir")
        _validate_path_component(filename, label="log filename")
        return self.ensure_log(subdir) / filename

    # ── Checkpoints (model artifacts, pipeline state) ───────────────────

    @property
    def checkpoints_root(self) -> Path:
        return self._checkpoints_root

    def checkpoint_dir(self, name: str) -> Path:
        """Create and return a checkpoint subdirectory for *name*
        (e.g.  ``"bm25"``)."""
        _validate_path_component(name, label="checkpoint name")
        d = self._checkpoints_root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def checkpoint_path(self, name: str, filename: str) -> Path:
        """Full path to a checkpoint file under ``checkpoints/<name>/<filename>``."""
        _validate_path_component(name, label="checkpoint name")
        _validate_path_component(filename, label="checkpoint filename")
        return self.checkpoint_dir(name) / filename

    # ── Backups (logical exports + file-level snapshots) ────────────

    @property
    def backups_root(self) -> Path:
        return self._backups_root

    def backup_dir(self, name: str = "logical") -> Path:
        """Create and return a backup subdirectory (e.g. ``logical``, ``snapshot``)."""
        _validate_path_component(name, label="backup name")
        d = self._backups_root / name
        d.mkdir(parents=True, exist_ok=True)
        return d


storage = Storage()


# ── Internal helpers ──────────────────────────────────────────────────────


def _validate_path_component(name: str, *, label: str = "component") -> str:
    """Reject path components that would escape their intended directory.

    A component must be a single, non-empty directory or file name — no ``/``,
    no ``..``, no leading ``.`` (to block hidden dirs like ``.ssh``), and no
    absolute paths.

    Args:
        name: The path component string to validate.
        label: Human-readable label for error messages.

    Returns:
        The validated *name*, unchanged on success.
    """
    if not name:
        raise ValueError(f"Path {label} must not be empty")
    if "/" in name or "\\" in name:
        raise ValueError(f"Path {label} must not contain path separators: {name!r}")
    if name in (".", ".."):
        raise ValueError(f"Path {label} must not be '{name}'")
    if name.startswith("."):
        raise ValueError(f"Path {label} must not start with '.': {name!r}")
    return name
