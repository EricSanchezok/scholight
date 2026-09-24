from __future__ import annotations

import pytest
from phase_calibrate import warm_envelopes


def samples():
    return [
        {
            "kind": kind,
            "round": trial,
            "completed": True,
            "stopped_by_probe": False,
            "peak_delta": 20 * 1024**2,
        }
        for kind, count in (("pdf_warm", 8), ("browser_warm", 4))
        for trial in range(5)
        for _ in range(count)
    ]


def test_warm_envelopes_keep_native_margin_and_all_five_rounds():
    result = warm_envelopes(samples())
    assert result["pdf_warm"] == {"fixed_bytes": 38 * 1024**2, "per_input_byte": 1}
    assert result["browser_warm"] == {"fixed_bytes": 38 * 1024**2, "per_input_byte": 0}


def test_warm_envelopes_reject_failed_or_missing_evidence():
    rows = samples()
    rows[0]["completed"] = False
    with pytest.raises(AssertionError, match="incomplete"):
        warm_envelopes(rows)
    with pytest.raises(AssertionError, match="five"):
        warm_envelopes(samples()[:-1])
