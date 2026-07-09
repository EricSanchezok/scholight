#!/usr/bin/env python3
"""Execute bulk delete of duplicate arXiv IDs from Milvus arxiv_papers.

Reads data/duplicates_to_delete.txt, deletes in batches.
"""

from __future__ import annotations

from pathlib import Path

import structlog

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_INPUT = _PROJECT_ROOT / "data" / "duplicates_to_delete.txt"

_DELETE_BATCH = 8000  # Milvus delete batch upper limit

logger = structlog.get_logger("delete-duplicates")


def run() -> None:
    from compass.store.client import get_client

    ids = [line.strip() for line in _INPUT.open() if line.strip()]
    logger.info("loaded delete targets", count=len(ids))

    client = get_client()
    deleted = 0
    failed = 0

    for i in range(0, len(ids), _DELETE_BATCH):
        chunk = ids[i : i + _DELETE_BATCH]
        try:
            result = client.delete("arxiv_papers", ids=chunk)
            n = result.get("delete_count", len(chunk))
            deleted += n
            if i % 40000 == 0:
                logger.info(
                    "delete progress", done=min(i + _DELETE_BATCH, len(ids)), total=len(ids)
                )
        except Exception:
            logger.exception("delete batch failed", first_id=chunk[0], batch_size=len(chunk))
            failed += len(chunk)

    logger.info("deletion done", deleted=deleted, failed=failed, total=len(ids))

    # Verify: check remaining paper count
    stats = client.get_collection_stats("arxiv_papers")
    logger.info("post-delete stats", paper_count=stats.get("row_count"))


if __name__ == "__main__":
    run()
