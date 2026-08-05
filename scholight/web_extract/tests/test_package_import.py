"""Import-boundary tests for the optional Web Extract runtime."""

from __future__ import annotations

import subprocess
import sys


def test_package_does_not_import_engine_eagerly() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import scholight.web_extract; "
                "raise SystemExit('scholight.web_extract.engine' in sys.modules)"
            ),
        ],
        check=False,
    )

    assert result.returncode == 0


def test_wire_contracts_do_not_import_extraction_engine() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import scholight.web_extract.contracts; "
                "raise SystemExit('scholight.web_extract.engine' in sys.modules)"
            ),
        ],
        check=False,
    )

    assert result.returncode == 0
