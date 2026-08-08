from __future__ import annotations

from collections.abc import Iterator

import pytest

from scholight.survey.workflow_runtime import _materialized_root, workflow_file


@pytest.fixture(autouse=True)
def _clear_workflow_cache() -> Iterator[None]:
    _materialized_root.cache_clear()
    yield
    _materialized_root.cache_clear()


def test_materializes_all_mcp_workflows_with_runtime_url() -> None:
    endpoint = "https://scholight.example/api/mcp"

    draft = workflow_file("draft.rcm", mcp_url=endpoint)
    runtime_root = draft.parents[1]

    assert f'url = "{endpoint}"' in draft.read_text(encoding="utf-8")
    for name in ("discovery.rcm", "expansion.rcm"):
        source = (runtime_root / "rcm" / name).read_text(encoding="utf-8")
        assert f'url = "{endpoint}"' in source
    assert (runtime_root / "prompts" / "draft.txt").is_file()
    assert workflow_file("draft.rcm", mcp_url=endpoint) == draft


@pytest.mark.parametrize("filename", ["../draft.rcm", "rcm/draft.rcm", "/tmp/draft.rcm"])
def test_rejects_unsafe_workflow_filename(filename: str) -> None:
    with pytest.raises(ValueError, match="basename"):
        workflow_file(filename, mcp_url="http://api:8000/mcp")


def test_rejects_unknown_workflow() -> None:
    with pytest.raises(ValueError, match="Unknown Survey workflow"):
        workflow_file("missing.rcm", mcp_url="http://api:8000/mcp")
