"""Chunkers: convert parsed content into evenly-sized chunks for embedding.

Two strategies:

- ``content_list_chunker`` — takes MinerU/Marker structured ``content_list``
  (dict with ``text_level`` headings), allocates chunk quota per section.
- ``md_chunker``          — takes raw Markdown strings (from LaTeX or PDF → md),
  uses recursive character split at paragraph/sentence boundaries.
"""

from __future__ import annotations

from scholight.pipeline.chunkers.content_list_chunker import (
    Chunk as Chunk,
    chunk_content_list as chunk_content_list,
)
from scholight.pipeline.chunkers.md_chunker import (
    MdChunk as MdChunk,
    chunk_markdown as chunk_markdown,
)

__all__ = [
    "Chunk",
    "MdChunk",
    "chunk_content_list",
    "chunk_markdown",
]
