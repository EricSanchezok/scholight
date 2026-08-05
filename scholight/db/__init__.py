"""Database access layer — asyncpg pool lifecycle + typed query functions."""

from __future__ import annotations

from typing import Any

from scholight.db.client import DBError, close_pool, create_pool, get_pool
from scholight.db.queries_history import (
    get_search_history,
    log_search,
    soft_delete_search_entry,
)


def __getattr__(name: str) -> Any:
    """Keep migration tooling available without loading it in runtime processes."""
    if name == "run_migrations":
        from scholight.db.migrate import run_migrations

        return run_migrations
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Client / lifecycle
    "create_pool",
    "close_pool",
    "get_pool",
    "DBError",
    # Migration
    "run_migrations",
    # History queries
    "log_search",
    "get_search_history",
    "soft_delete_search_entry",
]
