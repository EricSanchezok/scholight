"""Docker entrypoint: configure logging, then start uvicorn.

Why this file exists
--------------------
structlog's ``cache_logger_on_first_use=True`` freezes the processor chain
after the first ``get_logger()`` call.  Many modules call ``get_logger(__name__)``
at *import time* — including scholight.api.app, scholight.search, scholight.store.

If we used the CLI approach::

    uvicorn scholight.api.app:create_app --factory

then uvicorn would import ``scholight.api.app`` → trigger ``structlog.get_logger()``
→ freeze the processor chain → ``configure_logging()`` called too late.

The fix is to call ``configure_logging()`` first, *then* import the app and
start uvicorn — all in the same Python process.
"""

from __future__ import annotations

import os

from scholight.logging import configure_logging

# ── Configure structlog BEFORE any get_logger() calls happen ────────
# This must be the first call that touches structlog in the process.
configure_logging(
    log_level=os.environ.get("SCHOLIGHT_LOG_LEVEL", "INFO"),
    use_json=True,  # containers always output JSON to stdout
)

# ── Now safe to import modules that call get_logger(__name__) ───────
import uvicorn  # noqa: E402, I001
from scholight.api.app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("SCHOLIGHT_SERVER_HOST", "0.0.0.0"),
        port=int(os.environ.get("SCHOLIGHT_SERVER_PORT", "8000")),
        proxy_headers=os.environ.get("SCHOLIGHT_PROXY_HEADERS", "false").lower() == "true",
        forwarded_allow_ips=os.environ.get("SCHOLIGHT_FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
