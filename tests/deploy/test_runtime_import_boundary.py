"""Runtime images must not import migration-only Identity code during startup."""

from __future__ import annotations

import subprocess
import sys


def test_database_client_import_does_not_load_identity_migrations() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import scholight.db.client; "
            "raise SystemExit('sanchezcloud_identity.migrate' in sys.modules)",
        ],
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0


def test_logging_config_import_does_not_load_http_middleware() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from scholight.logging import configure_logging; "
            "raise SystemExit('scholight.logging.middleware' in sys.modules)",
        ],
        check=False,
        timeout=10,
    )

    assert completed.returncode == 0


def test_lazy_public_facades_remain_importable() -> None:
    from scholight.db import run_migrations
    from scholight.logging import RequestContextMiddleware, TimingMiddleware

    assert callable(run_migrations)
    assert RequestContextMiddleware.__name__ == "RequestContextMiddleware"
    assert TimingMiddleware.__name__ == "TimingMiddleware"
