"""Tests for query and document embedding input semantics."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from scholight.pipeline.embedder import Embedder


@pytest.mark.asyncio
async def test_embed_query_adds_academic_retrieval_instruction() -> None:
    embedder = Embedder()
    vector = [0.25, 0.5]

    with patch.object(embedder, "embed_batch", new=AsyncMock(return_value=[vector])) as embed_batch:
        result = await embedder.embed_query("deepseek")

    assert result == vector
    embed_batch.assert_awaited_once_with(
        [
            "Instruct: Given a research topic or paper title, retrieve relevant academic "
            "papers and their abstracts\nQuery:deepseek"
        ]
    )


@pytest.mark.asyncio
async def test_embed_queries_adds_instruction_to_each_query() -> None:
    embedder = Embedder()
    vectors = [[0.25, 0.5], [0.75, 1.0]]

    with patch.object(embedder, "embed_many", new=AsyncMock(return_value=vectors)) as embed_many:
        result = await embedder.embed_queries(["deepseek", "qwen"])

    assert result == vectors
    embed_many.assert_awaited_once_with(
        [
            "Instruct: Given a research topic or paper title, retrieve relevant academic "
            "papers and their abstracts\nQuery:deepseek",
            "Instruct: Given a research topic or paper title, retrieve relevant academic "
            "papers and their abstracts\nQuery:qwen",
        ]
    )


@pytest.mark.asyncio
async def test_embed_single_keeps_document_text_unmodified() -> None:
    embedder = Embedder()
    vector = [0.25, 0.5]
    document = "DeepSeek-V3 is a mixture-of-experts language model."

    with patch.object(embedder, "embed_batch", new=AsyncMock(return_value=[vector])) as embed_batch:
        result = await embedder.embed_single(document)

    assert result == vector
    embed_batch.assert_awaited_once_with([document])
