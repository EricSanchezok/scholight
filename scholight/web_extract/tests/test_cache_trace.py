from __future__ import annotations

import httpx
import pytest

from scholight.web_extract import service
from scholight.web_extract.tests.test_service import _Engine


@pytest.mark.asyncio
async def test_cache_trace_is_process_scoped_and_omits_credentialed_keys(monkeypatch) -> None:
    records = []

    def record(**values):
        trace = values["trace"]
        records.append((trace.cache_key_id, trace.cache_entry_bytes, values["cache_hit"]))

    monkeypatch.setattr(service, "log_completion", record)
    for _ in range(2):
        app = service.create_extract_service(engine=_Engine(), internal_token="fixture")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://extract"
        ) as client:
            for cookies in [{}, {}, {"session": "private-fixture"}]:
                response = await client.post(
                    "/v1/extract",
                    headers={"X-Scholight-Internal-Token": "fixture"},
                    json={"url": "https://example.com/private-path", "cookies": cookies},
                )
                assert response.status_code == 200
    assert records[0][0] == records[1][0] and len(records[0][0]) == 64
    assert records[0][0] != records[3][0]
    assert records[0][1] == records[1][1] and records[0][1] > 0
    assert records[1][2] is True
    assert records[2][:2] == (None, 0) and records[5][:2] == (None, 0)
