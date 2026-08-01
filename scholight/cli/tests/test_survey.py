"""Survey operator command contracts."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from scholight.cli.survey import _installed_rcm_version


def test_installed_rcm_version_accepts_reviewed_binary() -> None:
    completed = subprocess.CompletedProcess(
        args=["/usr/local/bin/accelerate", "--version"],
        returncode=0,
        stdout="accelerate 0.2.6\n",
        stderr="",
    )
    with patch("scholight.cli.survey.subprocess.run", return_value=completed):
        version = _installed_rcm_version()

    assert version == "0.2.6"


def test_installed_rcm_version_rejects_unreviewed_binary() -> None:
    completed = subprocess.CompletedProcess(
        args=["/usr/local/bin/accelerate", "--version"],
        returncode=0,
        stdout="accelerate 0.2.5\n",
        stderr="",
    )
    with (
        patch("scholight.cli.survey.subprocess.run", return_value=completed),
        pytest.raises(RuntimeError, match="reviewed release"),
    ):
        _installed_rcm_version()
