"""Database access layer — asyncpg pool lifecycle + typed query functions."""

from __future__ import annotations

from compass.db.client import DBError, close_pool, create_pool, get_pool
from compass.db.migrate import run_migrations
from compass.db.queries_history import (
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
    # History queries
    "log_search",
    "get_search_history",
    "soft_delete_search_entry",
]
