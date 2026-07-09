"""Docker entrypoint: configure logging, then start uvicorn.

Why this file exists
--------------------
structlog's ``cache_logger_on_first_use=True`` freezes the processor chain
after the first ``get_logger()`` call.  Many modules call ``get_logger(__name__)``
at *import time* — including compass.api.app, compass.search, compass.store.

If we used the CLI approach::

    uvicorn compass.api.app:create_app --factory

then uvicorn would import ``compass.api.app`` → trigger ``structlog.get_logger()``
→ freeze the processor chain → ``configure_logging()`` called too late.

The fix is to call ``configure_logging()`` first, *then* import the app and
start uvicorn — all in the same Python process.
"""

from __future__ import annotations

import os

from compass.logging import configure_logging

# ── Configure structlog BEFORE any get_logger() calls happen ────────
# This must be the first call that touches structlog in the process.
configure_logging(
    log_level=os.environ.get("COMPASS_LOG_LEVEL", "INFO"),
    use_json=True,  # containers always output JSON to stdout
)

# ── Now safe to import modules that call get_logger(__name__) ───────
import uvicorn
from compass.api.app import create_app

app = create_app()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=os.environ.get("COMPASS_SERVER_HOST", "0.0.0.0"),
        port=int(os.environ.get("COMPASS_SERVER_PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
