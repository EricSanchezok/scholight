"""Memory-isolated PDF child request contracts."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from scholight.survey.pdf_child import main


def test_pdf_child_accepts_only_a_local_request_path(tmp_path: Path) -> None:
    run_root = tmp_path / "run"
    run_root.mkdir()
    report = run_root / "08_survey.md"
    report.write_text("# Survey\n\nBody.", encoding="utf-8")
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "title": "Private title stays off argv",
                "generated_on": "2026-09-05",
                "asset_root": str(run_root),
                "markdown_path": str(report),
                "output_path": str(run_root / "08_survey.pdf"),
            }
        ),
        encoding="utf-8",
    )

    with (
        patch("scholight.survey.pdf_child._apply_memory_limit") as memory_limit,
        patch("scholight.survey.pdf_child.render_report_pdf_to_file") as render,
    ):
        result = main(["--request", str(request)])

    assert result == 0
    memory_limit.assert_called_once_with()
    assert render.call_args.kwargs["title"] == "Private title stays off argv"
    assert render.call_args.kwargs["cache_dir"] == tmp_path / "cache"
