"""Bounded streaming HTTP fetcher with redirect and DNS policy enforcement."""

from __future__ import annotations

import asyncio
import random
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver, ResolveResult

from scholight.web_extract.admission import BoundedGate, Permit
from scholight.web_extract.engine import ExtractInput, FetchResult
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.http_retry import (
    RETRY_STATUSES,
    FetchAttemptError,
    network_failure,
    retry_after,
    single_attempt,
)
from scholight.web_extract.policy import resolve_public_addresses, validate_public_target
from scholight.web_extract.spool import Spool
from scholight.web_extract.telemetry import current_trace, mime_category, phase

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_DEFAULT_HEADERS = {"User-Agent": "Scholight-Web-Extract/1.0"}


class PublicResolver(AbstractResolver):
    """Resolve once through the public-address policy used by the actual connection."""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        addresses = await resolve_public_addresses(host, port)
        return [
            {
                "hostname": host,
                "host": str(address),
                "port": port,
                "family": socket.AF_INET6 if address.version == 6 else socket.AF_INET,
                "proto": socket.IPPROTO_TCP,
                "flags": socket.AI_NUMERICHOST,
            }
            for address in addresses
            if family
            in {socket.AF_UNSPEC, socket.AF_INET6 if address.version == 6 else socket.AF_INET}
        ]

    async def close(self) -> None:
        return None


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    return parsed.scheme.lower(), parsed.hostname, port


def _cookie_header(cookies: dict[str, str]) -> str:
    jar = SimpleCookie()
    for name, value in cookies.items():
        jar[name] = value
    return "; ".join(morsel.OutputString() for morsel in jar.values())


class HttpFetcher:
    def __init__(
        self,
        *,
        validator: Callable[[str], Awaitable[object]] = validate_public_target,
        resolver: AbstractResolver | None = None,
        max_download_bytes: int = 50_000_000,
        timeout_seconds: float = 30.0,
        max_redirects: int = 8,
        concurrency: int = 16,
        spool: Spool | None = None,
        queueing: bool = False,
        admit: Callable[[], None] | None = None,
        reuse_connections: bool = False,
        retry_enabled: bool = False,
    ) -> None:
        self._validator = validator
        self._resolver = resolver
        self._max_download_bytes = max_download_bytes
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=10, sock_read=15)
        self._max_redirects = max_redirects
        self._gate = BoundedGate(
            "Download",
            capacity=concurrency,
            max_waiters=8 if queueing else 0,
            wait_seconds=2,
        )
        self._spool = spool
        self._admit = admit or (lambda: None)
        self._reuse_connections = reuse_connections
        self._connection_limit = concurrency
        self._connector: aiohttp.TCPConnector | None = None
        self._retry_enabled = retry_enabled

    def _connection_pool(self, *, reuse: bool) -> aiohttp.TCPConnector:
        if reuse and self._connector is not None and not self._connector.closed:
            return self._connector
        connector = aiohttp.TCPConnector(
            resolver=self._resolver or PublicResolver(),
            use_dns_cache=False,
            ttl_dns_cache=0,
            limit=self._connection_limit,
            limit_per_host=self._connection_limit,
            keepalive_timeout=15,
        )
        if reuse:
            self._connector = connector
        return connector

    async def close(self) -> None:
        connector, self._connector = self._connector, None
        if connector is not None:
            await connector.close()

    async def fetch(self, request: ExtractInput) -> FetchResult:
        await self._gate.acquire()
        permit = Permit(self._gate)
        fetched = None
        try:
            self._admit()
            fetched = await self._fetch_with_retry(request)
            if fetched.spool_file is not None:
                fetched.spool_file.seal()
                # Bound downloaded files waiting for the serial parser as well as I/O.
                return replace(fetched, permit=permit)
            permit.close()
            return fetched
        except BaseException:
            permit.close()
            if fetched is not None:
                fetched.close()
            raise

    async def _fetch_with_retry(self, request: ExtractInput) -> FetchResult:
        eligible = self._retry_enabled and not request.headers and not request.cookies
        deadline = time.monotonic() + (self._timeout.total or 30)
        trace = current_trace.get()
        if trace is not None:
            deadline = min(deadline, trace.deadline - 2)
        try:
            async with asyncio.timeout_at(deadline):
                for attempt in range(2):
                    self._admit()
                    if attempt and trace is not None:
                        trace.retry_count += 1
                    try:
                        return await self._fetch(request)
                    except FetchAttemptError as error:
                        if attempt or not eligible or not error.transient:
                            raise
                        # Server minimum delay may exceed the Full Jitter cap.
                        jitter = random.uniform(0, min(1.0, 0.2 * 2**attempt))  # nosec B311
                        delay = max(jitter, error.retry_delay or 0)
                        if delay >= deadline - time.monotonic():
                            raise
                        with phase("RetryLatency"):
                            await asyncio.sleep(delay)
        except TimeoutError as error:
            raise network_failure(error) from error
        raise AssertionError("retry loop exited without a result")

    @staticmethod
    @asynccontextmanager
    async def _response(
        session: aiohttp.ClientSession,
        url: str,
        headers: dict[str, str],
    ) -> AsyncIterator[aiohttp.ClientResponse]:
        async with session.get(url, headers=headers, allow_redirects=False) as response:
            trace = current_trace.get()
            if trace is not None:
                trace.upstream_status = response.status
                trace.mime = mime_category(response.headers.get("Content-Type", ""))
            try:
                yield response
            finally:
                if trace is not None:
                    trace.download_bytes += response.content.total_raw_bytes

    async def _fetch(self, request: ExtractInput) -> FetchResult:
        requested_url = request.url
        current_url = requested_url
        target_headers = dict(request.headers)
        headers = {**_DEFAULT_HEADERS, **target_headers}
        if request.cookies and not any(name.lower() == "cookie" for name in headers):
            headers["Cookie"] = _cookie_header(request.cookies)

        reuse = self._reuse_connections and not request.headers and not request.cookies
        connector = self._connection_pool(reuse=reuse)
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                connector_owner=not reuse,
                timeout=self._timeout,
                auto_decompress=True,
                trust_env=False,
                middlewares=(single_attempt,),
            ) as session:
                for redirect_count in range(self._max_redirects + 1):
                    await self._validator(current_url)
                    try:
                        async with self._response(session, current_url, headers) as response:
                            if response.status in _REDIRECTS and "Location" in response.headers:
                                if redirect_count >= self._max_redirects:
                                    raise ExtractError(
                                        code="too_many_redirects",
                                        message="Target exceeded the redirect limit.",
                                        status_code=502,
                                        retryable=False,
                                    )
                                next_url = urljoin(current_url, response.headers["Location"])
                                await self._validator(next_url)
                                if _origin(current_url) != _origin(next_url):
                                    headers = dict(_DEFAULT_HEADERS)
                                current_url = next_url
                                continue
                            if response.status >= 400:
                                raise FetchAttemptError(
                                    code="upstream_http_error",
                                    message=f"Target returned HTTP {response.status}.",
                                    status_code=502,
                                    retryable=response.status == 429 or response.status >= 500,
                                    transient=response.status in RETRY_STATUSES,
                                    retry_delay=retry_after(response.headers.get("Retry-After")),
                                )
                            body = bytearray()
                            body_file = (
                                self._spool.allocate(self._max_download_bytes)
                                if self._spool is not None
                                else None
                            )
                            try:
                                async for chunk in response.content.iter_chunked(64 * 1024):
                                    if body_file is not None:
                                        body_file.write(chunk)
                                    else:
                                        body.extend(chunk)
                                        if len(body) > self._max_download_bytes:
                                            raise ExtractError(
                                                code="response_too_large",
                                                message="Target response exceeds the download limit.",
                                                status_code=413,
                                                retryable=False,
                                            )
                            except BaseException:
                                if body_file is not None:
                                    body_file.close()
                                raise
                            return FetchResult(
                                requested_url=requested_url,
                                final_url=str(response.url),
                                status_code=response.status,
                                content_type=response.headers.get(
                                    "Content-Type", "application/octet-stream"
                                ),
                                charset=response.charset,
                                body=bytes(body),
                                spool_file=body_file,
                            )
                    except ExtractError:
                        raise
                    except (TimeoutError, aiohttp.ClientError) as exc:
                        raise network_failure(exc) from exc
        finally:
            if not reuse:
                await connector.close()
        raise AssertionError("redirect loop exited without a result")


__all__ = ["HttpFetcher", "PublicResolver"]
