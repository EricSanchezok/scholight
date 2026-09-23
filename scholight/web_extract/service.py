"""Internal-only ASGI service for browser and document extraction."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import uuid4

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from scholight.logging.emf import MetricUnit, emit_emf
from scholight.web_extract.cancellation import ClientDisconnectedError, until_disconnect
from scholight.web_extract.contracts import (
    InternalExtractRequest,
    InternalExtractResponse,
)
from scholight.web_extract.engine import ExtractDocument, ExtractInput
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.maintenance import cache_maintenance
from scholight.web_extract.retained_size import INDEX_BYTES, retained_size
from scholight.web_extract.telemetry import (
    ExtractTrace,
    current_trace,
    log_completion,
    mime_category,
    safe_request_id,
)


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
    def __init__(
        self,
        *,
        ttl_seconds: int,
        max_bytes: int,
        max_entries: int = 1024,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._max_bytes = max_bytes
        self._max_entries = max_entries
        self._clock = clock or (lambda: datetime.now(UTC))
        self._entries: OrderedDict[str, tuple[datetime, int, InternalExtractResponse]] = (
            OrderedDict()
        )
        self._bytes = 0
        self._trace_secret = secrets.token_bytes(32)

    def trace_id(self, key: str) -> str:
        return hmac.new(self._trace_secret, key.encode(), hashlib.sha256).hexdigest()

    def entry_size(self, key: str) -> int:
        entry = self._entries.get(key)
        return entry[1] if entry is not None else 0

    def prune(self) -> None:
        now = self._clock()
        for key, (expires_at, size, _value) in list(self._entries.items()):
            if expires_at <= now:
                self._entries.pop(key)
                self._bytes -= size
        while self._entries and (
            self._bytes > self._max_bytes or len(self._entries) > self._max_entries
        ):
            _key, (_expires_at, size, _value) = self._entries.popitem(last=False)
            self._bytes -= size

    def get(self, key: str) -> InternalExtractResponse | None:
        self.prune()
        entry = self._entries.get(key)
        if entry is None:
            return None
        self._entries.move_to_end(key)
        return entry[2]

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    def put(self, key: str, value: InternalExtractResponse) -> int:
        self.prune()
        expires_at = self._clock() + self._ttl
        size = retained_size((key, expires_at, value)) + INDEX_BYTES
        if size > self._max_bytes:
            return size
        previous = self._entries.pop(key, None)
        if previous is not None:
            self._bytes -= previous[1]
        self._entries[key] = (expires_at, size, value)
        self._bytes += size
        self.prune()
        return size


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
        trace = current_trace.get()
        download = (
            trace.download_bytes
            if trace is not None and trace.upstream_status
            else response.source_bytes
        )
        metrics["DownloadBytes"] = (0 if cache_hit else download, "Bytes")
        metrics["OutputBytes"] = (len(response.content.encode("utf-8")), "Bytes")
    if (trace := current_trace.get()) is not None:
        metrics.update({name: (value, "Milliseconds") for name, value in trace.phases.items()})
        metrics["SourceDocumentBytes"] = (trace.source_bytes, "Bytes")
        metrics["RenderedDOMBytes"] = (trace.dom_bytes, "Bytes")
    emit_emf(
        service="extract",
        outcome=outcome,
        metrics=metrics,
    )


async def _until_disconnect(
    engine: _Engine,
    request: InternalExtractRequest,
    http_request: Request,
) -> ExtractDocument:
    async def disconnected() -> None:
        while True:
            message = await http_request.receive()
            if message["type"] == "http.disconnect":
                return

    return await until_disconnect(
        engine.extract(
            ExtractInput(
                url=str(request.url),
                render=request.render,
                output=request.output,
                headers=request.headers,
                cookies=request.cookies,
            )
        ),
        disconnected,
    )


def create_extract_service(
    *,
    engine: _Engine,
    internal_token: str,
    cache_ttl_seconds: int = 600,
    cache_max_bytes: int = 32 * 1024 * 1024,
    admit: Callable[[], None] | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    cache = _SharedCache(ttl_seconds=cache_ttl_seconds, max_bytes=cache_max_bytes)

    @asynccontextmanager
    async def managed_lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with AsyncExitStack() as stack:
            if lifespan is not None:
                await stack.enter_async_context(lifespan(app))
            await stack.enter_async_context(cache_maintenance(cache.prune))
            yield

    app = FastAPI(
        title="Scholight Extract Service",
        docs_url=None,
        redoc_url=None,
        lifespan=managed_lifespan,
    )
    app.state.extract_cache = cache

    @app.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @app.post("/v1/extract", response_model=InternalExtractResponse)
    async def extract(
        request: InternalExtractRequest,
        http_request: Request,
        x_scholight_internal_token: str | None = Header(default=None),
        x_scholight_request_id: str | None = Header(default=None),
        x_scholight_budget_ms: str | None = Header(default=None),
    ) -> InternalExtractResponse | JSONResponse:
        if x_scholight_internal_token is None or not hmac.compare_digest(
            x_scholight_internal_token, internal_token
        ):
            return JSONResponse(status_code=401, content={"detail": {"code": "unauthorized"}})
        started_at = time.perf_counter()
        budget = 52.0
        if x_scholight_budget_ms is not None:
            try:
                supplied = int(x_scholight_budget_ms)
                budget = min(budget, max(0, supplied / 1000))
            except ValueError:
                pass  # Optional header; older peers retain the default deadline.
        trace = ExtractTrace(
            request_id=safe_request_id(x_scholight_request_id or str(uuid4())),
            deadline=time.monotonic() + budget,
        )
        context = current_trace.set(trace)
        cacheable = not request.headers and not request.cookies
        cache_hit = False
        response = None
        outcome = "unexpected_error"
        error: ExtractError | None = None
        try:
            if admit is not None:
                admit()
            key = _cache_key(request)
            if cacheable:
                trace.cache_key_id = cache.trace_id(key)
                trace.cache_ttl_seconds = cache_ttl_seconds
            if cacheable and (cached := cache.get(key)) is not None:
                response, cache_hit, outcome = cached, True, "cache_hit"
                trace.mime = mime_category(cached.content_type)
                trace.cache_entry_bytes = cache.entry_size(key)
                return cached
            if budget <= 2:
                raise TimeoutError
            # All work consumes one budget; up to two seconds remain for hard cleanup.
            async with asyncio.timeout(budget - 2):
                document = await _until_disconnect(engine, request, http_request)
            response = _response_from_document(document)
            trace.mime = mime_category(response.content_type)
            if cacheable:
                trace.cache_entry_bytes = cache.put(key, response)
            outcome = "browser_success" if response.rendered else "static_success"
            return response
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except ClientDisconnectedError:
            error = ExtractError(
                code="extract_cancelled",
                message="Client disconnected.",
                status_code=499,
                retryable=True,
            )
        except TimeoutError:
            error = ExtractError(
                code="extract_timeout",
                message="Web extraction exceeded the request deadline.",
                status_code=504,
                retryable=True,
            )
        except ExtractError as exc:
            error = exc
        except Exception:
            error = ExtractError(
                code="extract_worker_failed",
                message="Web extraction failed unexpectedly.",
                status_code=503,
                retryable=True,
            )
        finally:
            if error is not None:
                outcome = (
                    "cancelled" if error.code == "extract_cancelled" else f"error_{error.code}"
                )
                if error.status_code >= 500:
                    emit_emf(service="extract", metrics={"ExtractServiceFailure": (1, "Count")})
            _emit_extract_metrics(
                started_at=started_at,
                outcome=outcome,
                response=response,
                cache_hit=cache_hit,
            )
            log_completion(
                trace=trace,
                scope="internal",
                outcome=outcome,
                render=request.render.value,
                cache_eligible=cacheable,
                cache_hit=cache_hit,
            )
            current_trace.reset(context)
        return JSONResponse(
            status_code=error.status_code,
            content={
                "detail": {
                    "code": error.code,
                    "message": error.message,
                    "retryable": error.retryable,
                }
            },
        )

    return app


__all__ = ["create_extract_service"]
