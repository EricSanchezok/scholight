"""Internal-only ASGI service for browser and document extraction."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Protocol

from fastapi import FastAPI, Header
from fastapi.responses import JSONResponse

from scholight.logging.emf import MetricUnit, emit_emf
from scholight.web_extract.contracts import (
    InternalExtractRequest,
    InternalExtractResponse,
)
from scholight.web_extract.engine import ExtractDocument, ExtractInput
from scholight.web_extract.errors import ExtractError


class _Engine(Protocol):
    async def extract(self, request: ExtractInput) -> ExtractDocument: ...


def _response_from_document(document: ExtractDocument) -> InternalExtractResponse:
    return InternalExtractResponse(
        requested_url=document.requested_url,
        final_url=document.final_url,
        status_code=document.status_code,
        title=document.title,
        author=document.author,
        published_at=document.published_at,
        content_type=document.content_type,
        content=document.content,
        rendered=document.rendered,
        extractor=document.extractor,
        warnings=list(document.warnings),
        content_hash=document.content_hash,
        fetched_at=document.fetched_at,
        source_bytes=document.source_bytes,
    )


class _SharedCache:
    def __init__(self, *, ttl_seconds: int, max_bytes: int) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_bytes = max_bytes
        self._entries: OrderedDict[str, tuple[datetime, int, InternalExtractResponse]] = (
            OrderedDict()
        )
        self._bytes = 0

    def _prune(self) -> None:
        now = datetime.now(UTC)
        for key, (expires_at, size, _value) in list(self._entries.items()):
            if expires_at <= now:
                self._entries.pop(key)
                self._bytes -= size
        while self._bytes > self._max_bytes and self._entries:
            _key, (_expires_at, size, _value) = self._entries.popitem(last=False)
            self._bytes -= size

    def get(self, key: str) -> InternalExtractResponse | None:
        self._prune()
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[2]

    def put(self, key: str, value: InternalExtractResponse) -> None:
        self._prune()
        size = len(value.content.encode("utf-8"))
        if size > self._max_bytes:
            return
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes -= previous[1]
        self._entries[key] = (datetime.now(UTC) + self._ttl, size, value)
        self._bytes += size
        self._prune()


def _cache_key(request: InternalExtractRequest) -> str:
    payload = json.dumps(
        {
            "url": str(request.url),
            "render": request.render.value,
            "output": request.output.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _emit_extract_metrics(
    *,
    started_at: float,
    outcome: str,
    response: InternalExtractResponse | None = None,
    cache_hit: bool = False,
) -> None:
    metrics: dict[str, tuple[float | int, MetricUnit]] = {
        "RequestCount": (1, "Count"),
        "Latency": ((time.perf_counter() - started_at) * 1000, "Milliseconds"),
        "CacheHit": (int(cache_hit), "Count"),
    }
    if response is not None:
        metrics["DownloadBytes"] = (response.source_bytes, "Bytes")
        metrics["OutputBytes"] = (len(response.content.encode("utf-8")), "Bytes")
    emit_emf(
        service="extract",
        outcome=outcome,
        metrics=metrics,
    )


def create_extract_service(
    *,
    engine: _Engine,
    internal_token: str,
    cache_ttl_seconds: int = 600,
    cache_max_bytes: int = 256 * 1024 * 1024,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Scholight Extract Service",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    cache = _SharedCache(ttl_seconds=cache_ttl_seconds, max_bytes=cache_max_bytes)

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/v1/extract", response_model=InternalExtractResponse)
    async def extract(
        request: InternalExtractRequest,
        x_scholight_internal_token: str | None = Header(default=None),
    ) -> InternalExtractResponse | JSONResponse:
        if x_scholight_internal_token is None or not hmac.compare_digest(
            x_scholight_internal_token, internal_token
        ):
            return JSONResponse(status_code=401, content={"detail": {"code": "unauthorized"}})
        started_at = time.perf_counter()
        key = _cache_key(request)
        cacheable = not request.headers and not request.cookies
        if cacheable and (cached := cache.get(key)) is not None:
            _emit_extract_metrics(
                started_at=started_at,
                outcome="cache_hit",
                response=cached,
                cache_hit=True,
            )
            return cached
        try:
            document = await engine.extract(
                ExtractInput(
                    url=str(request.url),
                    render=request.render,
                    output=request.output,
                    headers=request.headers,
                    cookies=request.cookies,
                )
            )
        except ExtractError as exc:
            _emit_extract_metrics(started_at=started_at, outcome=f"error_{exc.code}")
            if exc.status_code >= 500:
                emit_emf(
                    service="extract",
                    metrics={"ExtractServiceFailure": (1, "Count")},
                )
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "detail": {
                        "code": exc.code,
                        "message": exc.message,
                        "retryable": exc.retryable,
                    }
                },
            )
        response = _response_from_document(document)
        if cacheable:
            cache.put(key, response)
        _emit_extract_metrics(
            started_at=started_at,
            outcome="browser_success" if response.rendered else "static_success",
            response=response,
        )
        return response

    return app


__all__ = ["create_extract_service"]
