from __future__ import annotations

import pytest
from calibrate import calibration_cases, recommended


@pytest.mark.parametrize("size", [1000, 10_000, 100_000])
def test_scaled_pdf_keeps_all_calibration_text_on_visible_pages(size: int) -> None:
    import pymupdf

    case = next(c for c in calibration_cases() if c.name == f"pdf-stream-{size}")
    with pymupdf.open(stream=case.body, filetype="pdf") as document:
        words = [word[4] for page in document for word in page.get_text("words")]
    assert words == ["Evidence"] * (size // 9)


def test_failed_sample_cannot_be_silently_excluded_from_memory_recommendation() -> None:
    rows = [
        {"kind": kind, "input_bytes": 1000, "peak_delta": 1000, "completed": True}
        for kind in ("html", "pdf", "text")
    ]
    rows.append({"kind": "pdf", "input_bytes": 100_000, "peak_delta": 1, "completed": False})
    with pytest.raises(ValueError, match="incomplete"):
        recommended(rows)


def test_memory_recommendation_requires_cold_worker_samples_and_accounts_for_them() -> None:
    rows = [
        {"kind": kind, "input_bytes": 1000, "peak_delta": 1000, "completed": True}
        for kind in ("html", "pdf", "text")
    ]
    with pytest.raises(ValueError, match="startup"):
        recommended(rows)
    rows.extend(
        {"kind": kind, "input_bytes": 0, "peak_delta": 20 * 1024**2, "completed": True}
        for kind in ("parser_startup", "browser_startup")
    )
    result = recommended(rows)
    for kind in ("parser_startup", "browser_startup"):
        assert result[kind]["fixed_bytes"] == 38 * 1024**2
        assert result[kind]["samples"] == 1
