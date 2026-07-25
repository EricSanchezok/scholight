"""Resource-boundary tests for the Pandoc conversion stage."""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from scholight.pipeline.latex_md import LatexResourceLimitError, _run_pandoc


def _completed(returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=["pandoc"], returncode=returncode)


def test_pandoc_rejects_oversized_input_before_start() -> None:
    with patch("scholight.pipeline.latex_md.subprocess.run") as run:
        with pytest.raises(LatexResourceLimitError, match="input exceeds"):
            _run_pandoc("x" * (16 * 1024 * 1024 + 1))

    run.assert_not_called()


def test_pandoc_uses_sandbox_heap_limit_and_file_output(tmp_path: Path) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        output_path = Path(command[command.index("-o") + 1])
        output_path.write_text("# converted", encoding="utf-8")
        assert "--sandbox" in command
        assert command[-3:] == ["+RTS", "-M512M", "-RTS"]
        assert kwargs["stdout"] is subprocess.DEVNULL
        return _completed()

    with (
        patch("scholight.pipeline.latex_md.tempfile.gettempdir", return_value=str(tmp_path)),
        patch("scholight.pipeline.latex_md.subprocess.run", side_effect=fake_run),
    ):
        assert _run_pandoc("\\documentclass{article}") == "# converted"


def test_pandoc_rejects_oversized_output(tmp_path: Path) -> None:
    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        output_path = Path(command[command.index("-o") + 1])
        with output_path.open("wb") as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
        return _completed()

    with (
        patch("scholight.pipeline.latex_md.tempfile.gettempdir", return_value=str(tmp_path)),
        patch("scholight.pipeline.latex_md.subprocess.run", side_effect=fake_run),
    ):
        with pytest.raises(LatexResourceLimitError, match="output exceeds"):
            _run_pandoc("\\documentclass{article}")


def test_pandoc_timeout_is_resource_limit() -> None:
    with patch(
        "scholight.pipeline.latex_md.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["pandoc"], timeout=60),
    ):
        with pytest.raises(LatexResourceLimitError, match="timed out"):
            _run_pandoc("\\documentclass{article}")


def test_pandoc_sigkill_is_resource_limit() -> None:
    with patch(
        "scholight.pipeline.latex_md.subprocess.run",
        return_value=_completed(-signal.SIGKILL),
    ):
        with pytest.raises(LatexResourceLimitError, match="resource limit"):
            _run_pandoc("\\documentclass{article}")


def test_pandoc_heap_exhaustion_is_resource_limit(tmp_path: Path) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        stderr = kwargs["stderr"]
        assert hasattr(stderr, "write")
        stderr.write(b"pandoc: Heap exhausted\n")
        stderr.flush()
        return _completed(251)

    with (
        patch("scholight.pipeline.latex_md.tempfile.gettempdir", return_value=str(tmp_path)),
        patch("scholight.pipeline.latex_md.subprocess.run", side_effect=fake_run),
    ):
        with pytest.raises(LatexResourceLimitError, match="resource limit"):
            _run_pandoc("\\documentclass{article}")
