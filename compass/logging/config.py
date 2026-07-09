"""Core logging configuration for Academic Compass.

Responsibilities:
  - One-time structlog + stdlib wiring via `configure_logging()`
  - Async-safe context propagation via `structlog.contextvars`
  - Automatic JSON (server/production) vs console (CLI/dev) output
  - Unifies ALL log output — structlog, pymilvus, httpx, uvicorn —
    through a single ProcessorFormatter pipeline
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from compass.logging.cleanup import tame_third_party_loggers

if TYPE_CHECKING:
    from structlog.typing import Processor

# Detect orjson for faster JSON rendering.
try:
    import orjson as _orjson
except ImportError:
    _orjson = None  # type: ignore[assignment]


# ------------------------------------------------------------------
# Renderer selection
# ------------------------------------------------------------------


def _build_renderer(*, use_json: bool | None = None) -> Processor:
    """Pick the output renderer.

    - None (default): auto-detect — JSON when COMPASS_LOG_JSON=1
      or stderr is not a terminal; otherwise colored console.
    - True: always JSON.
    - False: always console.
    """
    if use_json is None:
        use_json = os.environ.get("COMPASS_LOG_JSON") == "1" or not sys.stderr.isatty()

    if use_json:
        if _orjson is not None:
            # orjson returns bytes — wrap to str for ProcessorFormatter compat.
            def _orjson_dumps(obj: object, default: Callable[[Any], Any] | None = None) -> str:
                return _orjson.dumps(obj, default=default).decode("utf-8")

            return structlog.processors.JSONRenderer(serializer=_orjson_dumps)
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer(colors=True)


# ------------------------------------------------------------------
# configure_logging
# ------------------------------------------------------------------


def configure_logging(
    *,
    log_level: str = "INFO",
    use_json: bool | None = None,
    file_handler: tuple[str, int, int] | None = None,
    mode: str = "a",
) -> None:
    """Configure structlog + stdlib logging for the whole application.

    Call exactly once at startup. Subsequent calls are no-ops because
    structlog's `cache_logger_on_first_use=True` means the processor
    chain is frozen after the first `get_logger()` call.

    Args:
        log_level: Minimum log level (DEBUG, INFO, WARNING, ERROR).
        use_json: Override the auto-detect JSON/console choice.
        file_handler: Optional (filename, max_bytes, backup_count) tuple.
            When provided, logs are written to a RotatingFileHandler at
            the given path instead of stdout.  The parent directory is
            created automatically if it does not exist.
        mode: File open mode for the RotatingFileHandler.
            ``"a"`` (append, default) or ``"w"`` (overwrite on each run).
    """
    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)
    renderer = _build_renderer(use_json=use_json)

    # These processors apply to BOTH structlog logs AND third-party
    # stdlib logs (via foreign_pre_chain in ProcessorFormatter).
    shared_processors: list[Processor] = [
        # MUST be first — merges contextvars into every log entry so
        # request_id, method, path, peer propagate to pymilvus/httpx/...
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        timestamper,
    ]

    # --- structlog side ---
    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.processors.CallsiteParameterAdder(
                {
                    structlog.processors.CallsiteParameter.FILENAME,
                    structlog.processors.CallsiteParameter.FUNC_NAME,
                    structlog.processors.CallsiteParameter.LINENO,
                }
            ),
            # Hand off to ProcessorFormatter for final rendering —
            # this is what allows structlog and stdlib logs to share
            # the same renderer below.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
        context_class=dict,
    )

    # --- stdlib side (captures pymilvus, httpx, asyncpg, uvicorn, ...) ---
    formatter = structlog.stdlib.ProcessorFormatter(
        # Pre-processing for third-party log entries:
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler: logging.Handler
    if file_handler is not None:
        from logging.handlers import RotatingFileHandler

        fname, max_bytes, backup_count = file_handler
        log_path = Path(fname)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            str(log_path),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            mode=mode,
        )
    else:
        handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    # force=True replaces any existing root-logger config.
    logging.basicConfig(handlers=[handler], level=log_level.upper(), force=True)

    # Silence chatty third-party libraries.
    tame_third_party_loggers()
