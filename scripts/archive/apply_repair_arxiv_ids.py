#!/usr/bin/env python3
"""Apply arXiv ID repairs from data/arxiv_id_repair_map.jsonl.

For each repair pair (old_id → new_id):
  1. Read the full paper record from Milvus (using old_id)
  2. Insert with new_id (upsert — arxiv_id is the PK)
  3. Delete old_id

Verification: before inserting, checks if new_id already exists.
If it does, logs a conflict and skips that pair (manual review needed).
"""

from __future__ import annotations

import json
from pathlib import Path

import structlog

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_MAP_FILE = _PROJECT_ROOT / "data" / "arxiv_id_repair_map.jsonl"

_FULL_FIELDS = [
    "arxiv_id",
    "title",
    "authors",
    "abstract",
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
    "has_latex",
    "has_pdf",
    "has_markdown",
    "has_content_list",
    "has_chunks",
    "images_count",
    "abstract_embedding",
    "abstract_sparse",
    "title_sparse",
]

logger = structlog.get_logger("apply-repair")


def apply() -> None:
    from scholight.store.client import get_client

    client = get_client()

    pairs: list[dict[str, str]] = []
    with _MAP_FILE.open() as f:
        for line in f:
            pairs.append(json.loads(line.strip()))

    logger.info("loaded repair map", total=len(pairs))

    # Phase 1: check conflicts — any new_id already in use?
    new_ids = [p["new"] for p in pairs]
    conflicts: set[str] = set()
    for i in range(0, len(new_ids), 2000):
        chunk = new_ids[i : i + 2000]
        rows = client.query(
            "arxiv_papers",
            filter=f"arxiv_id in {json.dumps(chunk)}",
            output_fields=["arxiv_id"],
            limit=len(chunk) + 10,
        )
        for row in rows:
            conflicts.add(row["arxiv_id"])

    if conflicts:
        conflict_pairs = [p for p in pairs if p["new"] in conflicts]
        logger.warning(
            "new IDs already exist — skipping",
            count=len(conflict_pairs),
            first=conflict_pairs[:5],
        )
        pairs = [p for p in pairs if p["new"] not in conflicts]

    logger.info("ready after conflict check", to_repair=len(pairs), conflicts=len(conflicts))

    # Phase 2: read old records, insert with new_id, delete old
    inserted = 0
    deleted = 0
    failed = 0

    for i, p in enumerate(pairs):
        old_id, new_id = p["old"], p["new"]
        try:
            # Read
            rows = client.query(
                "arxiv_papers",
                filter=f"arxiv_id == '{old_id}'",
                output_fields=_FULL_FIELDS,
                limit=1,
            )
            if not rows:
                logger.warning("old record not found", arxiv_id=old_id)
                failed += 1
                continue

            record = rows[0]
            record["arxiv_id"] = new_id  # <— rename

            # Insert
            client.upsert("arxiv_papers", data=[record])

            # Delete old
            client.delete("arxiv_papers", ids=[old_id])

            inserted += 1
            deleted += 1

        except Exception:
            logger.exception("repair failed", old_id=old_id, new_id=new_id)
            failed += 1

        if (i + 1) % 200 == 0:
            logger.info("repair progress", done=i + 1, total=len(pairs))

    logger.info("repair done", inserted=inserted, deleted=deleted, failed=failed)

    # Verify
    stats = client.get_collection_stats("arxiv_papers")
    logger.info("post-repair stats", paper_count=stats.get("row_count"))


if __name__ == "__main__":
    apply()
