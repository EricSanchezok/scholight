"""Third-party logger taming.

Every library we depend on (httpx, pymilvus, asyncpg, uvicorn etc.) uses
stdlib logging at DEBUG internally.  In production this would flood stdout
with gRPC frame details, TCP handshake traces, and connection pool chatter.

This module sets reasonable default levels for each noisy logger.
Levels can be overridden via environment variables:
  COMPASS_LOG_{LIBRARY}=DEBUG
"""

import logging
import os

# ------------------------------------------------------------------
# Level table
# ------------------------------------------------------------------

_NOISY_LOGGERS: list[tuple[str, int]] = [
    ("httpcore", logging.WARNING),
    ("httpx", logging.WARNING),
    ("milvus_model", logging.WARNING),
    ("nltk", logging.WARNING),
    ("pymilvus", logging.WARNING),
    ("asyncpg.connection", logging.WARNING),
    ("asyncpg.pool", logging.WARNING),
    ("uvicorn.access", logging.INFO),
]


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------


def tame_third_party_loggers() -> None:
    """Set default log levels for all known noisy libraries.

    Individual loggers can be overridden with env vars:
        COMPASS_LOG_HTTPX=DEBUG
    """
    for logger_name, default_level in _NOISY_LOGGERS:
        env_name = f"COMPASS_LOG_{logger_name.split('.')[0].upper()}"
        level = _resolve_level(env_name, default_level)
        logging.getLogger(logger_name).setLevel(level)


def _resolve_level(env_var: str, default: int) -> int:
    value = os.environ.get(env_var)
    if value is None:
        return default
    numeric = getattr(logging, value.upper(), None)
    if isinstance(numeric, int):
        return numeric
    return default
