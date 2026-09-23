from __future__ import annotations

import pytest
from calibrate import recommended


def test_failed_sample_cannot_be_silently_excluded_from_memory_recommendation() -> None:
    rows = [
        {"kind": kind, "input_bytes": 1000, "peak_delta": 1000, "completed": True}
        for kind in ("html", "pdf", "text")
    ]
    rows.append({"kind": "pdf", "input_bytes": 100_000, "peak_delta": 1, "completed": False})
    with pytest.raises(ValueError, match="incomplete"):
        recommended(rows)
