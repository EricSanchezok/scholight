"""Chunkers: convert parsed content into evenly-sized chunks for embedding.

Primary strategy:

- ``md_chunker`` — takes raw Markdown strings (from LaTeX or PDF → md),
  uses recursive character split at paragraph/sentence boundaries.
"""

from __future__ import annotations

from scholight.pipeline.chunkers.md_chunker import (
    MdChunk as MdChunk,
    chunk_markdown as chunk_markdown,
)

__all__ = [
    "MdChunk",
    "chunk_markdown",
]
