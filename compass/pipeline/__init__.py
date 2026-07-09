"""Pipeline: PDF parsing → chunking → embedding generation.

All imports are lazy to avoid pulling in heavy dependencies (MinerU, httpx,
milvus_model) when only a subset of the pipeline is needed — e.g. the
``parse_to_markdown`` script only uses ``latex_md`` and ``pdf_md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from compass.pipeline.chunkers import (
        Chunk as Chunk,
        MdChunk as MdChunk,
        chunk_content_list as chunk_content_list,
        chunk_markdown as chunk_markdown,
    )
    from compass.pipeline.embedder import Embedder as Embedder
    from compass.pipeline.latex_md import (
        LatexMdError as LatexMdError,
        latex_to_markdown as latex_to_markdown,
    )
    from compass.pipeline.parser import (
        MinerUParseError as MinerUParseError,
        MinerUTimeoutError as MinerUTimeoutError,
        content_list_to_markdown as content_list_to_markdown,
        parse_pdf as parse_pdf,
    )
    from compass.pipeline.pdf_md import PDFMdError as PDFMdError, pdf_to_markdown as pdf_to_markdown


def __getattr__(name: str):
    """Lazy import — only load submodules when actually accessed."""
    import importlib

    _MODULES = {
        "Chunk": "compass.pipeline.chunkers",
        "chunk_content_list": "compass.pipeline.chunkers",
        "Embedder": "compass.pipeline.embedder",
        "LatexMdError": "compass.pipeline.latex_md",
        "latex_to_markdown": "compass.pipeline.latex_md",
        "MdChunk": "compass.pipeline.chunkers",
        "chunk_markdown": "compass.pipeline.chunkers",
        "MinerUParseError": "compass.pipeline.parser",
        "MinerUTimeoutError": "compass.pipeline.parser",
        "content_list_to_markdown": "compass.pipeline.parser",
        "parse_pdf": "compass.pipeline.parser",
        "PDFMdError": "compass.pipeline.pdf_md",
        "pdf_to_markdown": "compass.pipeline.pdf_md",
    }

    if name in _MODULES:
        mod = importlib.import_module(_MODULES[name])
        return getattr(mod, name)
    raise AttributeError(f"module 'compass.pipeline' has no attribute {name!r}")


__all__ = [
    "Chunk",
    "Embedder",
    "LatexMdError",
    "MdChunk",
    "MinerUParseError",
    "MinerUTimeoutError",
    "PDFMdError",
    "chunk_content_list",
    "chunk_markdown",
    "content_list_to_markdown",
    "latex_to_markdown",
    "parse_pdf",
    "pdf_to_markdown",
]
