"""Contract tests for the dependency-free Scholight search Skill CLI."""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from types import ModuleType
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from scholight.api.models.search import PublicSearchRequest

_SCRIPT = Path(__file__).parents[2] / "skills" / "scholight-search" / "scripts" / "search.py"


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


@pytest.fixture
def cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location("scholight_skill_search", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_search_builds_public_api_payload_and_access_key_header(
    cli: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}
    upstream = b'{"query":"retrieval","hits":[],"result_count":0}\n'

    def open_stub(request: Request, *, timeout: float) -> _Response:
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["payload"] = json.loads(request.data or b"null")
        captured["timeout"] = timeout
        return _Response(upstream)

    monkeypatch.setattr(cli, "urlopen", open_stub)
    exit_code = cli.main(
        [
            "search",
            "retrieval",
            "--strength",
            "thorough",
            "--limit",
            "7",
            "--category",
            "cs.AI",
            "--category",
            "cs.IR",
            "--author",
            "Jane Doe",
            "--date-from",
            "2024-01-01",
            "--date-to",
            "2025-01-01",
        ],
        environ={
            "SCHOLIGHT_API_URL": "https://example.com/api/",
            "SCHOLIGHT_API_KEY": "sk_live_secret",
        },
    )

    output = capsys.readouterr()
    assert (exit_code, output.out, output.err) == (0, upstream.decode(), "")
    assert captured["url"] == "https://example.com/api/search"
    assert captured["timeout"] == 30.0
    assert captured["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer sk_live_secret",
        "Content-type": "application/json",
    }
    payload = captured["payload"]
    assert isinstance(payload, dict)
    PublicSearchRequest.model_validate(payload)
    assert payload == {
        "query": "retrieval",
        "strength": "thorough",
        "limit": 7,
        "filters": {
            "categories": ["cs.AI", "cs.IR"],
            "authors": ["Jane Doe"],
            "date_from": "2024-01-01",
            "date_to": "2025-01-01",
        },
    }


def test_anonymous_search_omits_authorization_header(
    cli: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def open_stub(request: Request, *, timeout: float) -> _Response:
        captured.update(dict(request.header_items()))
        return _Response(b"{}")

    monkeypatch.setattr(cli, "urlopen", open_stub)
    exit_code = cli.main(
        ["search", "retrieval"],
        environ={"SCHOLIGHT_API_URL": "https://example.com/api"},
    )

    assert exit_code == 0
    assert "Authorization" not in captured


def test_missing_api_url_has_stable_configuration_exit(
    cli: ModuleType,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(["search", "retrieval"], environ={})

    output = capsys.readouterr()
    assert exit_code == 2
    assert output.out == ""
    assert "SCHOLIGHT_API_URL is required" in output.err


@pytest.mark.parametrize(("status", "exit_code"), [(401, 3), (403, 3), (429, 4), (500, 5)])
def test_http_failures_have_stable_exit_codes(
    cli: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: int,
    exit_code: int,
) -> None:
    def open_stub(request: Request, *, timeout: float) -> _Response:
        raise HTTPError(request.full_url, status, "failure", {}, io.BytesIO(b'{"detail":"x"}'))

    monkeypatch.setattr(cli, "urlopen", open_stub)
    actual = cli.main(
        ["search", "retrieval"],
        environ={"SCHOLIGHT_API_URL": "https://example.com/api"},
    )

    output = capsys.readouterr()
    assert actual == exit_code
    assert output.out == ""
    assert f"HTTP {status}" in output.err


@pytest.mark.parametrize("failure", [URLError("offline"), TimeoutError("timed out")])
def test_network_and_timeout_failures_have_stable_exit(
    cli: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
) -> None:
    def open_stub(request: Request, *, timeout: float) -> _Response:
        raise failure

    monkeypatch.setattr(cli, "urlopen", open_stub)
    exit_code = cli.main(
        ["search", "retrieval"],
        environ={"SCHOLIGHT_API_URL": "https://example.com/api"},
    )

    output = capsys.readouterr()
    assert exit_code == 5
    assert output.out == ""
    assert output.err.startswith("Network error:")
