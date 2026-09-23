from __future__ import annotations

import json

from cache_replay import real_trace


def test_real_replay_excludes_server_assigned_canary_ids_and_unknown_hit_cost(tmp_path) -> None:
    rows = []
    for index, (identifier, hit, cost) in enumerate(
        [
            ("unknown-hit", True, 1),
            ("natural-miss", False, 80),
            ("server-canary-uuid", False, 900),
            ("natural-hit", True, 2),
        ]
    ):
        rows.append(
            {
                "event": "extract_completed",
                "scope": "internal",
                "request_id": identifier,
                "cache_eligible": True,
                "cache_key_id": "a" * 64,
                "cache_hit": hit,
                "cache_entry_bytes": 1024,
                "duration_ms": cost,
                "outcome": "cache_hit" if hit else "static_success",
                "timestamp": f"2026-09-23T10:00:0{index}+00:00",
            }
        )
    path = tmp_path / "trace.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    accesses, source = real_trace(path, {"server-canary-uuid"})
    assert [access.cost_ms for access in accesses] == [80, 80]
    assert source["excluded"]["canary"] == 1 and source["excluded"]["unknown_cost"] == 1
