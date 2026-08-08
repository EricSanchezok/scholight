"""Materialize immutable Survey workflows with deployment-specific endpoints."""

from __future__ import annotations

import json
import shutil
import tempfile
from functools import lru_cache
from pathlib import Path

_SOURCE_ROOT = Path(__file__).parent / "workflow"
_MCP_URL_SENTINEL = 'url = "http://api:8000/mcp"'


@lru_cache(maxsize=8)
def _materialized_root(mcp_url: str) -> Path:
    """Copy the workflow tree once and replace only the declared MCP endpoint."""
    target = Path(tempfile.mkdtemp(prefix="scholight-survey-workflow-"))
    shutil.copytree(_SOURCE_ROOT, target, dirs_exist_ok=True)
    replacement = f"url = {json.dumps(mcp_url)}"
    replaced = 0
    for rcm_file in sorted(target.rglob("*.rcm")):
        source = rcm_file.read_text(encoding="utf-8")
        if _MCP_URL_SENTINEL not in source:
            continue
        rcm_file.write_text(source.replace(_MCP_URL_SENTINEL, replacement), encoding="utf-8")
        replaced += 1
    if replaced == 0:
        raise RuntimeError("Survey workflow MCP endpoint sentinel was not found")
    return target


def workflow_file(filename: str, *, mcp_url: str) -> Path:
    """Return a safe runtime workflow path while preserving relative includes."""
    relative = Path(filename)
    if relative.is_absolute() or ".." in relative.parts or relative.parent != Path("."):
        raise ValueError("Survey workflow filename must be a basename")
    path = _materialized_root(mcp_url) / "rcm" / relative
    if not path.is_file():
        raise ValueError(f"Unknown Survey workflow: {filename}")
    return path
