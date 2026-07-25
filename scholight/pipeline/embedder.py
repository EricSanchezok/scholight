"""Embedder: generates embeddings via SiliconFlow Qwen3-Embedding-0.6B API.

OpenAI-compatible ``/v1/embeddings`` endpoint.  Supports single-text, batch,
and chunk-pipeline embedding with configurable concurrency.
"""

from __future__ import annotations

import asyncio

import httpx
import structlog
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from scholight.config import settings
from scholight.utils.http import is_transient

logger = structlog.get_logger(__name__)

_ACADEMIC_QUERY_INSTRUCTION = (
    "Given a research topic or paper title, retrieve relevant academic papers and their abstracts"
)
_api_client: httpx.AsyncClient | None = None


def _auth_headers() -> dict[str, str]:
    if settings.embedding_api_key:
        return {"Authorization": f"Bearer {settings.embedding_api_key}"}
    return {}


def create_embedding_client() -> httpx.AsyncClient:
    """Create one bounded client suitable for API-lifecycle reuse."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.embedding_connect_timeout_seconds,
            read=settings.embedding_read_timeout_seconds,
            write=settings.embedding_write_timeout_seconds,
            pool=settings.embedding_pool_timeout_seconds,
        ),
        limits=httpx.Limits(
            max_connections=settings.embedding_max_connections,
            max_keepalive_connections=settings.embedding_max_keepalive_connections,
        ),
        headers=_auth_headers(),
    )


async def start_api_embedding_client() -> None:
    """Create the shared API embedding client once."""
    global _api_client
    if _api_client is None:
        _api_client = create_embedding_client()


async def stop_api_embedding_client() -> None:
    """Close and clear the shared API embedding client."""
    global _api_client
    client, _api_client = _api_client, None
    if client is not None:
        await client.aclose()


def _instruct_query(query: str) -> str:
    return f"Instruct: {_ACADEMIC_QUERY_INSTRUCTION}\nQuery:{query}"


class Embedder:
    """Async embedding client for SiliconFlow Qwen3-Embedding-0.6B.

    Query embeddings use :meth:`embed_query`, which applies Qwen3's
    instruction-aware retrieval format. Document and chunk embeddings use
    :meth:`embed_single` or :meth:`embed_many` with their text unchanged.

    Usage::

        async with Embedder() as e:
            query_vec = await e.embed_query("some research topic")
            document_vec = await e.embed_single("some abstract")
            batches = await e.embed_batch(["text1", "text2"])
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = False

    async def __aenter__(self) -> Embedder:
        if self._client is None and _api_client is not None:
            self._client = _api_client
        if self._client is None:
            self._client = create_embedding_client()
            self._owns_client = True
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
        self._client = None
        self._owns_client = False

    @property
    def _url(self) -> str:
        return f"{settings.embedding_base_url}/embeddings"

    # ── Core API ───────────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception(is_transient),
        reraise=True,
    )
    async def _embed_request(self, texts: list[str]) -> list[list[float]]:
        if self._client is None:
            raise RuntimeError("Embedder not opened; use 'async with Embedder() as e:'")

        body = {"input": texts, "model": settings.embedding_model}
        logger.debug("embedding request", batch_size=len(texts))
        resp = await self._client.post(self._url, json=body)
        resp.raise_for_status()
        data = resp.json()
        embeddings = [item["embedding"] for item in data["data"]]

        if len(embeddings) != len(texts):
            raise ValueError(f"got {len(embeddings)} embeddings for {len(texts)} texts")

        expected = settings.embedding_dim
        for i, emb in enumerate(embeddings):
            if len(emb) != expected:
                raise ValueError(f"dim mismatch at idx {i}: got {len(emb)}, expected {expected}")

        return embeddings

    async def embed_query(self, query: str) -> list[float]:
        """Embed one retrieval query using Qwen3's instruction-aware format."""
        return (await self.embed_batch([_instruct_query(query)]))[0]

    async def embed_queries(self, queries: list[str]) -> list[list[float]]:
        """Embed retrieval queries in batches using the same query instruction."""
        return await self.embed_many([_instruct_query(query) for query in queries])

    async def embed_single(self, text: str) -> list[float]:
        return (await self.embed_batch([text]))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return await self._embed_request(texts)

    # ── Pipeline integration ───────────────────────────────────────

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Embed a large number of texts with batching + concurrency control."""
        if not texts:
            return []

        batch_size = settings.embedding_batch_size
        sem = asyncio.Semaphore(settings.embedding_concurrency)

        async def _batch(b: list[str]) -> list[list[float]]:
            async with sem:
                return await self.embed_batch(b)

        batches = [texts[i : i + batch_size] for i in range(0, len(texts), batch_size)]
        results = await asyncio.gather(*[_batch(b) for b in batches])

        all_embeddings: list[list[float]] = []
        for r in results:
            all_embeddings.extend(r)
        return all_embeddings
