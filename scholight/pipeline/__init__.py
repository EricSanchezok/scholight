"""Pipeline: PDF parsing → chunking → embedding generation.

All imports are lazy to avoid pulling in heavy dependencies (MinerU, httpx,
milvus_model) when only a subset of the pipeline is needed — e.g. the
``parse_to_markdown`` script only uses ``latex_md`` and ``pdf_md``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scholight.pipeline.chunkers import (
        Chunk as Chunk,
        MdChunk as MdChunk,
        chunk_content_list as chunk_content_list,
        chunk_markdown as chunk_markdown,
    )
    from scholight.pipeline.embedder import Embedder as Embedder
    from scholight.pipeline.latex_md import (
        LatexMdError as LatexMdError,
        latex_to_markdown as latex_to_markdown,
    )
    from scholight.pipeline.parser import (
        MinerUParseError as MinerUParseError,
        MinerUTimeoutError as MinerUTimeoutError,
        content_list_to_markdown as content_list_to_markdown,
        parse_pdf as parse_pdf,
    )
    from scholight.pipeline.pdf_md import (
        PDFMdError as PDFMdError,
        pdf_to_markdown as pdf_to_markdown,
    )


def __getattr__(name: str) -> object:
    """Lazy import — only load submodules when actually accessed."""
    import importlib

    _MODULES = {
        "Chunk": "scholight.pipeline.chunkers",
        "chunk_content_list": "scholight.pipeline.chunkers",
        "Embedder": "scholight.pipeline.embedder",
        "LatexMdError": "scholight.pipeline.latex_md",
        "latex_to_markdown": "scholight.pipeline.latex_md",
        "MdChunk": "scholight.pipeline.chunkers",
        "chunk_markdown": "scholight.pipeline.chunkers",
        "MinerUParseError": "scholight.pipeline.parser",
        "MinerUTimeoutError": "scholight.pipeline.parser",
        "content_list_to_markdown": "scholight.pipeline.parser",
        "parse_pdf": "scholight.pipeline.parser",
        "PDFMdError": "scholight.pipeline.pdf_md",
        "pdf_to_markdown": "scholight.pipeline.pdf_md",
    }

    if name in _MODULES:
        mod = importlib.import_module(_MODULES[name])
        return getattr(mod, name)
    raise AttributeError(f"module 'scholight.pipeline' has no attribute {name!r}")


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
