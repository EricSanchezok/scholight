from __future__ import annotations

import json

import httpx
import pytest
from canary import Canary, private_read, private_write


def test_failed_network_call_is_recorded_without_credentials(monkeypatch) -> None:
    canary = Canary("https://example.org")
    canary.client.close()
    monkeypatch.setattr("canary.time.sleep", lambda _delay: None)

    def timeout(request):
        raise httpx.ReadTimeout("fixture timeout", request=request)

    canary.client = httpx.Client(base_url=canary.base, transport=httpx.MockTransport(timeout))
    with pytest.raises(RuntimeError):
        canary.call("POST", "/api/extract", token="private-fixture-key", label="probe")
    assert canary.records[0]["status"] == 0
    assert "private-fixture-key" not in json.dumps(canary.records)


def test_canary_records_server_ids_and_never_exceeds_one_call_per_ten_seconds(monkeypatch) -> None:
    clock = [100.0]
    starts = []

    def sleep(delay):
        clock[0] += delay

    def reply(_request):
        starts.append(clock[0])
        return httpx.Response(
            200, json={"secret": "private-response"}, headers={"X-Request-ID": "server-id"}
        )

    monkeypatch.setattr("canary.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("canary.time.sleep", sleep)
    canary = Canary("https://example.org")
    canary.client.close()
    canary.client = httpx.Client(base_url=canary.base, transport=httpx.MockTransport(reply))
    for _ in range(3):
        canary.call("POST", "/api/extract", token="private-request", label="probe")
    assert starts == [100, 110, 120]
    assert all(r["request_id"] == "server-id" for r in canary.records)
    assert "private-" not in json.dumps(canary.records)


def test_private_state_requires_owner_only_access(tmp_path) -> None:
    path = tmp_path / "private.json"
    private_write(path, {"key": "fixture"})
    assert private_read(path) == {"key": "fixture"}
    path.chmod(0o644)
    with pytest.raises(AssertionError):
        private_read(path)
