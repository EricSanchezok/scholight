"""Zilliz Cloud client connection management — thread-safe singleton with lazy init."""

from __future__ import annotations

import atexit
import contextlib
import threading
from collections.abc import Iterator
from typing import TypeVar

import structlog
from pymilvus import MilvusClient as PyMilvusClient

from compass.config import settings

_T = TypeVar("_T")

logger = structlog.get_logger(__name__)

# ── Module-level state ────────────────────────────────────────────────────────
_client: PyMilvusClient | None = None
_lock: threading.RLock = threading.RLock()
_close_registered: bool = False

# MilvusClient is not re-entrant for write operations.
# All insert/delete must acquire this lock before calling the client.
_WRITE_LOCK: threading.Lock = threading.Lock()

# ── Scale-tuning constants ────────────────────────────────────────────────────
CONNECT_TIMEOUT: int = 30

# Search queries use eventual consistency for throughput at scale.
SEARCH_CONSISTENCY = "Eventually"
# Exact lookups and deletions need strong consistency.
QUERY_CONSISTENCY = "Strong"
DELETE_CONSISTENCY = "Strong"


# ── URI resolution ────────────────────────────────────────────────────────────


def _resolve_uri() -> str:
    """Return the Zilliz Cloud cluster endpoint from settings."""
    return settings.zilliz_uri


def _resolve_token() -> str:
    """Return the Zilliz Cloud API token from settings."""
    return settings.zilliz_token


# ── Lifecycle ─────────────────────────────────────────────────────────────────


def _auto_close() -> None:
    """atexit handler — close the client if connected."""
    global _client
    if _client is not None:
        with contextlib.suppress(Exception):
            _client.close()
        _client = None


def connect() -> PyMilvusClient:
    """Initialise the Zilliz Cloud client singleton explicitly.

    Called automatically by :func:`get_client` on first use.
    Safe to call multiple times — subsequent calls are no-ops.
    Thread-safe via double-checked locking.
    """
    global _client, _close_registered

    if _client is not None:
        return _client

    with _lock:
        if _client is not None:
            return _client

        uri = _resolve_uri()
        token = _resolve_token()
        masked = f"{token[:8]}…{token[-4:]}" if len(token) > 12 else "[not set]"
        logger.info("connecting to zilliz", uri=uri, token=masked, timeout=CONNECT_TIMEOUT)

        _client = PyMilvusClient(uri=uri, token=token, timeout=CONNECT_TIMEOUT)

        if not _close_registered:
            atexit.register(_auto_close)
            _close_registered = True

        logger.info("zilliz client connected", uri=uri)
        return _client


def close() -> None:
    """Close the Zilliz Cloud client and clear the singleton."""
    global _client
    with _lock:
        if _client is not None:
            try:
                _client.close()
                logger.info("zilliz client closed")
            except Exception as exc:
                logger.warning("error closing zilliz client", error=str(exc))
            finally:
                _client = None


# ── Public API ────────────────────────────────────────────────────────────────


def get_client() -> PyMilvusClient:
    """Return the global Zilliz Cloud client singleton, connecting lazily on first call.

    Thread-safe via double-checked locking.  Read-only operations (search/query)
    may share the singleton across threads; write operations (insert/delete)
    must hold :data:`_WRITE_LOCK`.
    """
    global _client
    if _client is not None:
        return _client

    with _lock:
        if _client is not None:
            return _client
        return connect()


def is_connected() -> bool:
    """Return ``True`` if the Zilliz Cloud client singleton is reachable.

    Lazily initialises the client if it has not yet been connected.
    """
    try:
        client = get_client()
        # Reachability probe: a successful call means the client is wired
        # and the cluster responds.  The collection list itself is unused.
        _ = client.list_collections()
        return True
    except Exception:
        return False


# ── Shared utilities ──────────────────────────────────────────────────────────


def batched(items: list[_T], batch_size: int = 1000) -> Iterator[list[_T]]:
    """Yield successive *batch_size*-sized slices from *items*."""
    for i in range(0, len(items), batch_size):
        yield items[i : i + batch_size]


def escape_sql(val: str) -> str:
    """Escape a value for safe use in a single-quoted Milvus filter string."""
    return val.replace("'", "\\'")
