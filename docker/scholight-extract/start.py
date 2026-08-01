"""Start the isolated Scholight Web Extract service."""

from __future__ import annotations

import os

from scholight.logging import configure_logging

configure_logging(
    log_level=os.environ.get("SCHOLIGHT_LOG_LEVEL", "INFO"),
    use_json=True,
)

import uvicorn  # noqa: E402

from scholight.config import settings  # noqa: E402
from scholight.web_extract.runtime import build_extract_app  # noqa: E402

app = build_extract_app()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=settings.extract_server_host,
        port=settings.extract_server_port,
        access_log=False,
    )
