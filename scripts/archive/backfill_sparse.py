"""backfill_sparse.py — BM25-encode existing arxiv_papers sparse vectors.

Loads all paper abstracts from Milvus, fits BM25 IDF statistics, then
streams every paper through the encoder (abstract + title) and partial-updates
the resulting ``abstract_sparse`` / ``title_sparse`` fields via ``partial_update=True``.

Two-phase design (resumable):
  1. fit  — learn IDF, save checkpoint to ``checkpoints/bm25/arxiv.pkl``
  2. fill — encode + upsert sparse vectors for all papers

Usage::

    python scripts/backfill_sparse.py fit
    python scripts/backfill_sparse.py fill
    python scripts/backfill_sparse.py fit --sample-rate 5   # fit on every 5th paper
    python scripts/backfill_sparse.py fill --batch-size 2000 --dry-run
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import structlog

_project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_project_root))

from scholight.config import settings  # noqa: E402
from scholight.logging import configure_logging  # noqa: E402
from scholight.pipeline.sparse_encoder import SparseEncoder  # noqa: E402
from scholight.storage import storage  # noqa: E402
from scholight.store.client import get_client  # noqa: E402

_LOGGER = structlog.get_logger(__name__)

# Milvus query cursor limit — keeps per-batch memory bounded.
_QUERY_LIMIT = 10_000

# Default upsert batch size (Milvus-recommended cap: 1000).
_UPSERT_BATCH_SIZE = 1000


# ═══════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BM25 sparse vector backfill for arxiv_papers")
    sp = p.add_subparsers(dest="command", required=True)

    # ---- fit ----
    p_fit = sp.add_parser("fit", help="Learn BM25 IDF from a sample of abstracts")
    p_fit.add_argument(
        "--sample-rate",
        type=int,
        default=1,
        help="Fit on every Nth paper (default 1 = all; use 5 for ~650k sample)",
    )

    # ---- fill ----
    p_fill = sp.add_parser("fill", help="Encode all papers and upsert sparse vectors")
    p_fill.add_argument(
        "--batch-size",
        type=int,
        default=_UPSERT_BATCH_SIZE,
        help=f"Documents per Milvus upsert call (default {_UPSERT_BATCH_SIZE})",
    )
    p_fill.add_argument("--dry-run", action="store_true", help="Encode but do not write to Milvus")

    return p.parse_args()


# ═══════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════

_CHECKPOINT_PATH = storage.checkpoint_path("bm25", "arxiv.pkl")


def _iter_papers(
    output_fields: list[str] | None = None,
    *,
    limit: int = _QUERY_LIMIT,
) -> tuple[str, str]:
    """Cursor-based Milvus scanner — yields (arxiv_id, abstract, title).

    Each yielded row has all three fields as plain strings (empty if null).
    """
    if output_fields is None:
        output_fields = ["abstract", "title"]
    client = get_client()

    last_id = ""
    while True:
        flt = f"arxiv_id > '{last_id}'" if last_id else "arxiv_id != ''"
        rows = client.query(
            "arxiv_papers",
            filter=flt,
            output_fields=output_fields,
            limit=limit,
        )
        if not rows:
            break
        for row in rows:
            aid = row.get("arxiv_id", "")
            abstract = row.get("abstract", "") or ""
            title = row.get("title", "") or ""
            yield aid, abstract, title
            last_id = aid

        _LOGGER.debug("cursor batch", last_id=last_id, batch_size=len(rows))


def _build_fit_sample(sample_rate: int) -> list[str]:
    """Stream abstracts from Milvus, keeping every *sample_rate*-th one."""
    abstracts: list[str] = []
    _LOGGER.info("building fit sample", sample_rate=sample_rate)
    for i, (_aid, abstract, _title) in enumerate(_iter_papers()):
        if i % sample_rate == 0 and abstract:
            abstracts.append(abstract)
    _LOGGER.info("fit sample ready", total=len(abstracts))
    return abstracts


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1: fit
# ═══════════════════════════════════════════════════════════════════════════


def run_fit(args: argparse.Namespace) -> None:
    _LOGGER.info("fit starting", sample_rate=args.sample_rate)
    t0 = time.perf_counter()

    corpus = _build_fit_sample(args.sample_rate)
    if not corpus:
        _LOGGER.error("no abstracts found — is arxiv_papers populated?")
        sys.exit(1)

    encoder = SparseEncoder(num_workers=4)  # NLTK tokenizer parallelism during fit
    encoder.fit(corpus)

    _CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)
    encoder.save_versioned(str(_CHECKPOINT_PATH), keep=3)

    elapsed = time.perf_counter() - t0
    _LOGGER.info(
        "fit done",
        vocab_size=encoder.dim,
        sample_size=len(corpus),
        checkpoint=str(_CHECKPOINT_PATH),
        elapsed_s=round(elapsed, 1),
    )


# ═══════════════════════════════════════════════════════════════════════════
# Phase 2: fill
# ═══════════════════════════════════════════════════════════════════════════


def _flush(
    encoder: SparseEncoder,
    client: object,
    arxiv_ids: list[str],
    abstracts: list[str],
    titles: list[str],
    dry: bool,
) -> int:
    """Encode a single batch and partial_update to Milvus (sparse vectors only).

    Uses ``partial_update=True`` so only ``arxiv_id`` (PK) + sparse vectors
    need to be sent — all other fields are preserved automatically.
    Returns number of rows written.
    """
    n = len(arxiv_ids)
    abstract_vecs = encoder.encode(abstracts, batch_size=n)
    title_vecs = encoder.encode(titles, batch_size=n)

    data: list[dict] = []
    for i in range(n):
        data.append(
            {
                "arxiv_id": arxiv_ids[i],
                "abstract_sparse": abstract_vecs[i],
                "title_sparse": title_vecs[i],
            }
        )

    if dry:
        _LOGGER.info("upsert (dry-run)", count=n, sample_id=arxiv_ids[0])
        return n

    try:
        result = client.upsert("arxiv_papers", data=data, partial_update=True)
        upserted = result.get("upsert_count", n)
        _LOGGER.debug("upsert ok", count=n, upserted=upserted)
        return n
    except Exception:
        _LOGGER.exception("upsert failed", count=n, sample_id=arxiv_ids[0])
        return 0


def run_fill(args: argparse.Namespace) -> None:
    """Encode all papers and partial-upsert sparse vectors.

    Uses ``partial_update=True`` so only ``arxiv_id`` + sparse vectors are
    sent — all other fields are preserved by Milvus automatically.
    """
    if not _CHECKPOINT_PATH.exists():
        _LOGGER.error("checkpoint not found — run 'fit' first", path=str(_CHECKPOINT_PATH))
        sys.exit(1)

    encoder = SparseEncoder.load(str(_CHECKPOINT_PATH))
    _LOGGER.info("encoder loaded", vocab_size=encoder.dim)

    batch_size = args.batch_size
    dry = args.dry_run
    client = get_client()

    t0 = time.perf_counter()
    total = 0
    errors = 0
    id_batch: list[str] = []
    abs_batch: list[str] = []
    tit_batch: list[str] = []

    for arxiv_id, abstract, title in _iter_papers():
        id_batch.append(arxiv_id)
        abs_batch.append(abstract)
        tit_batch.append(title)

        if len(id_batch) >= batch_size:
            n = _flush(encoder, client, id_batch, abs_batch, tit_batch, dry)
            total += n
            errors += batch_size - n
            id_batch.clear()
            abs_batch.clear()
            tit_batch.clear()

            if total % 100_000 == 0 and total > 0:
                _LOGGER.info("fill progress", total=total)

    if id_batch:
        n = _flush(encoder, client, id_batch, abs_batch, tit_batch, dry)
        total += n
        errors += len(id_batch) - n

    elapsed = time.perf_counter() - t0
    _LOGGER.info("fill done", total=total, errors=errors, elapsed_s=round(elapsed, 1))


# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════


def main() -> None:
    args = _parse_args()

    configure_logging(
        log_level=settings.log_level,
        use_json=True,
        file_handler=(str(storage.log_path("sparse_backfill")), 50_000_000, 3),
    )
    # 3rd-party loggers are already silenced in scholight.logging.cleanup

    if args.command == "fit":
        run_fit(args)
    elif args.command == "fill":
        run_fill(args)


if __name__ == "__main__":
    main()
