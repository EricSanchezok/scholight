#!/usr/bin/env python3
"""Migrate arxiv_papers — add has_latex/has_pdf/has_markdown/has_content_list/images_count.

  1. Export all ~3M rows (including embeddings) to JSON backup on GPFS.
  2. Drop the collection.
  3. Recreate with new schema (including 5 flag fields).
  4. Re-insert all rows with flags defaulted to false/0, embeddings preserved.

Usage:
    python scripts/migrate_papers_add_flags.py              # full migration
    python scripts/migrate_papers_add_flags.py --dry-run    # export backup only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import structlog

_project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_project_root))

from scholight.config import settings  # noqa: E402
from scholight.logging import configure_logging  # noqa: E402
from scholight.storage import storage  # noqa: E402
from scholight.store.client import get_client  # noqa: E402
from scholight.store.schema import ARXIV_PAPERS_SCHEMA  # noqa: E402

_LOG_FILE = storage.log_path("migrate_flags", "migrate.log")
configure_logging(
    log_level=settings.log_level,
    use_json=True,
    file_handler=(str(_LOG_FILE), 50_000_000, 3),
)
logger = structlog.get_logger("migrate-flags")

BACKUP_DIR = Path(settings.data_root) / ".migration_backup"
BACKUP_PATH = BACKUP_DIR / "arxiv_papers_flags_backup.json"
BATCH_SIZE = 5000

# Fields we fetch from the old collection and re-insert as-is.
_EXPORT_FIELDS = [
    "arxiv_id",
    "title",
    "abstract",
    "authors",
    "categories",
    "created",
    "updated",
    "version",
    "updated_history",
    "license",
    "comments",
    "doi",
    "journal_ref",
    "acm_class",
    "abstract_embedding",
    "abstract_sparse",
    "title_sparse",
]
_ARRAY_KEYS = frozenset({"authors", "categories", "updated_history"})
# Sparse dicts from Milvus may arrive as other container types—normalize.
_SPARSE_KEYS = frozenset({"abstract_sparse", "title_sparse"})


def _escape(val: str) -> str:
    return val.replace("'", "\\'")


def _normalize_row(r: dict[str, Any]) -> dict[str, Any]:
    """Convert Milvus-specific types to plain Python for JSON serialization and re-insert."""
    for k in _ARRAY_KEYS:
        v = r.get(k)
        if v is not None and not isinstance(v, list):
            r[k] = list(v)
    for k in _SPARSE_KEYS:
        v = r.get(k)
        if v is not None and not isinstance(v, dict):
            r[k] = dict(v) if hasattr(v, "items") else {}
    return r


def export_rows() -> tuple[list[dict], int]:
    client = get_client()
    rows: list[dict] = []
    last_id = ""

    while True:
        flt = f"arxiv_id > '{_escape(last_id)}'" if last_id else "arxiv_id != ''"
        batch = client.query(
            "arxiv_papers",
            filter=flt,
            output_fields=_EXPORT_FIELDS,
            limit=BATCH_SIZE,
        )
        if not batch:
            break
        for r in batch:
            _normalize_row(r)
        rows.extend(batch)
        last_id = batch[-1]["arxiv_id"]
        logger.info("exporting", count=len(rows), last=last_id)

    # JSON backup — only scalar + array fields (exclude embeddings, too large)
    _backup_fields = [
        f
        for f in _EXPORT_FIELDS
        if f not in ("abstract_embedding", "abstract_sparse", "title_sparse")
    ]
    backuproot = [{k: r[k] for k in _backup_fields if k in r} for r in rows]
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_PATH.write_text(json.dumps(backuproot, ensure_ascii=False), encoding="utf-8")
    logger.info("backup written", path=str(BACKUP_PATH), count=len(rows))
    return rows, len(rows)


def recreate_collection(client) -> None:
    # Safety: backup MUST exist before we drop anything.
    if not BACKUP_PATH.exists() or BACKUP_PATH.stat().st_size == 0:
        raise RuntimeError(
            f"Backup file missing or empty at {BACKUP_PATH} — refusing to drop collection"
        )
    logger.info("backup confirmed — dropping arxiv_papers")
    client.drop_collection("arxiv_papers")

    logger.info("creating arxiv_papers with new schema")
    client.create_collection(
        collection_name="arxiv_papers",
        schema=ARXIV_PAPERS_SCHEMA,
        consistency_level="Strong",
    )

    from scholight.store.schema import _build_arxiv_papers_indexes, _wait_for_index

    indexes = _build_arxiv_papers_indexes()
    client.create_index("arxiv_papers", index_params=indexes)
    for idx in indexes:
        _wait_for_index(client, "arxiv_papers", idx.index_name)

    client.load_collection("arxiv_papers")
    logger.info("arxiv_papers recreated + indexed + loaded")


def reinsert_rows(rows: list[dict]) -> None:
    client = get_client()
    total = len(rows)

    for i in range(0, total, BATCH_SIZE):
        batch = rows[i : i + BATCH_SIZE]
        data: list[dict[str, Any]] = []
        for r in batch:
            row: dict[str, Any] = {k: r.get(k) for k in _EXPORT_FIELDS}
            row["has_latex"] = False
            row["has_pdf"] = False
            row["has_markdown"] = False
            row["has_content_list"] = False
            row["has_chunks"] = False
            row["images_count"] = 0
            # Ensure vector fields are non-empty
            if not row.get("abstract_embedding"):
                row["abstract_embedding"] = _ZERO_VEC
            if not row.get("abstract_sparse"):
                row["abstract_sparse"] = {}
            if not row.get("title_sparse"):
                row["title_sparse"] = {}
            data.append(row)

        client.upsert("arxiv_papers", data=data)
        progress = min(i + BATCH_SIZE, total)
        logger.info("re-inserting", progress=progress, total=total)


def verify_rows() -> None:
    client = get_client()
    total = 0
    last_id = ""
    while True:
        flt = f"arxiv_id > '{_escape(last_id)}'" if last_id else "arxiv_id != ''"
        batch = client.query(
            "arxiv_papers",
            filter=flt,
            output_fields=[
                "arxiv_id",
                "has_latex",
                "has_pdf",
                "has_markdown",
                "has_content_list",
                "images_count",
            ],
            limit=BATCH_SIZE,
        )
        if not batch:
            break
        total += len(batch)
        for r in batch[:3]:
            assert r["has_latex"] is False, f"unexpected has_latex: {r}"
            assert r["has_pdf"] is False, f"unexpected has_pdf: {r}"
            assert r["has_markdown"] is False, f"unexpected has_markdown: {r}"
            assert r["has_content_list"] is False, f"unexpected has_content_list: {r}"
            assert r["images_count"] == 0, f"unexpected images_count: {r}"
        last_id = batch[-1]["arxiv_id"]
        if total % 500000 == 0:
            logger.info("verifying", count=total)

    logger.info("migration complete — %d rows, all flags false/0, embeddings preserved", total)


def run(dry_run: bool = False) -> None:
    client = get_client()
    if "arxiv_papers" not in client.list_collections():
        logger.info("arxiv_papers not found — nothing to do")
        return

    rows, count = export_rows()
    logger.info("export complete", count=count)

    if dry_run:
        logger.info("DRY RUN — backup at %s, %d rows exported", str(BACKUP_PATH), count)
        return

    recreate_collection(client)
    reinsert_rows(rows)
    verify_rows()


_ZERO_VEC: list[float] = []


def _init():
    from scholight.config import settings

    global _ZERO_VEC
    _ZERO_VEC = [0.0] * settings.embedding_dim


if __name__ == "__main__":
    _init()
    parser = argparse.ArgumentParser(description="Migrate arxiv_papers to add flag fields")
    parser.add_argument("--dry-run", action="store_true", help="Export backup only, do not modify")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
