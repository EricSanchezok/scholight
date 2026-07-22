"""Database access layer — asyncpg pool lifecycle + typed query functions."""

from __future__ import annotations

from scholight.db.client import DBError, close_pool, create_pool, get_pool
from scholight.db.migrate import run_migrations
from scholight.db.migrate_all import run_all_migrations
from scholight.db.queries_history import (
    get_search_history,
    log_search,
    soft_delete_search_entry,
)

__all__ = [
    # Client / lifecycle
    "create_pool",
    "close_pool",
    "get_pool",
    "DBError",
    # Migration
    "run_migrations",
    "run_all_migrations",
    # History queries
    "log_search",
    "get_search_history",
    "soft_delete_search_entry",
]
