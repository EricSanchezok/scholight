"""Isolated URL fetching and clean-content extraction.

The API process imports cache and transport types from this package without
running the local extraction engine. Keep the public convenience exports lazy
so the API image does not need Chromium-only extraction dependencies.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scholight.web_extract.engine import (
        ExtractEngine as ExtractEngine,
        ExtractInput as ExtractInput,
    )
    from scholight.web_extract.errors import ExtractError as ExtractError


def __getattr__(name: str) -> object:
    """Load optional extraction implementations only when explicitly requested."""
    import importlib

    modules = {
        "ExtractEngine": "scholight.web_extract.engine",
        "ExtractInput": "scholight.web_extract.engine",
        "ExtractError": "scholight.web_extract.errors",
    }
    if name in modules:
        module = importlib.import_module(modules[name])
        return getattr(module, name)
    raise AttributeError(f"module 'scholight.web_extract' has no attribute {name!r}")


__all__ = ["ExtractEngine", "ExtractError", "ExtractInput"]
