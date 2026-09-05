"""One-render-per-process entry point for memory-isolated Survey PDFs."""

from __future__ import annotations

import argparse
import json
import resource
from datetime import date
from pathlib import Path

from scholight.config import settings
from scholight.survey.report_pdf import ReportPdfError, render_report_pdf_to_file


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render one trusted local Survey report")
    parser.add_argument("--request", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _apply_memory_limit()
        request_path = args.request.resolve(strict=True)
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ReportPdfError("PDF child request must be an object")
        title = _required_text(payload, "title", maximum=160)
        generated_on = date.fromisoformat(_required_text(payload, "generated_on", maximum=10))
        asset_root = Path(_required_text(payload, "asset_root", maximum=4096))
        markdown_path = Path(_required_text(payload, "markdown_path", maximum=4096))
        output_path = Path(_required_text(payload, "output_path", maximum=4096))
        cache_dir = request_path.parent / "cache"
        render_report_pdf_to_file(
            title=title,
            markdown_path=markdown_path,
            output_path=output_path,
            asset_root=asset_root,
            cache_dir=cache_dir,
            generated_on=generated_on,
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError, ReportPdfError):
        return 1
    return 0


def _apply_memory_limit() -> None:
    limit = settings.survey_pdf_memory_limit_mib * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))


def _required_text(payload: dict[str, object], key: str, *, maximum: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ReportPdfError(f"PDF child request has invalid {key}")
    return value


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
