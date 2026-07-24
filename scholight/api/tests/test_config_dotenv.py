"""Configuration isolation contracts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_disable_dotenv_ignores_repository_environment_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("SCHOLIGHT_ZILLIZ_TOKEN=must-not-load\n")
    env = os.environ.copy()
    env.pop("SCHOLIGHT_ZILLIZ_TOKEN", None)
    env["SCHOLIGHT_DISABLE_DOTENV"] = "1"

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from scholight.config import settings; "
            "raise SystemExit(0 if settings.zilliz_token == '' else 1)",
        ],
        cwd=tmp_path,
        env=env,
        check=False,
    )

    assert result.returncode == 0
