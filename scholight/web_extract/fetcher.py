"""Bounded streaming HTTP fetcher with redirect and DNS policy enforcement."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Awaitable, Callable
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from scholight.web_extract.engine import ExtractInput, FetchResult
from scholight.web_extract.errors import ExtractError
from scholight.web_extract.policy import resolve_public_addresses, validate_public_target

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})


class PublicResolver(AbstractResolver):
    """Resolve once through the public-address policy used by the actual connection."""

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[dict[str, Any]]:
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


def _strip_cross_origin_credentials(headers: dict[str, str]) -> dict[str, str]:
    return {
        name: value for name, value in headers.items() if name.lower() not in _SENSITIVE_HEADERS
    }


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
    ) -> None:
        self._validator = validator
        self._resolver = resolver
        self._max_download_bytes = max_download_bytes
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=10, sock_read=15)
        self._max_redirects = max_redirects
        self._semaphore = asyncio.Semaphore(concurrency)

    async def fetch(self, request: ExtractInput) -> FetchResult:
        async with self._semaphore:
            return await self._fetch(request)

    async def _fetch(self, request: ExtractInput) -> FetchResult:
        requested_url = request.url
        current_url = requested_url
        headers = dict(request.headers)
        headers.setdefault("User-Agent", "Scholight-Web-Extract/1.0")
        if request.cookies and not any(name.lower() == "cookie" for name in headers):
            headers["Cookie"] = _cookie_header(request.cookies)

        resolver = self._resolver or PublicResolver()
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=False,
            ttl_dns_cache=0,
        )
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=self._timeout,
                auto_decompress=True,
            ) as session:
                for redirect_count in range(self._max_redirects + 1):
                    await self._validator(current_url)
                    try:
                        async with session.get(
                            current_url,
                            headers=headers,
                            allow_redirects=False,
                        ) as response:
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
                                    headers = _strip_cross_origin_credentials(headers)
                                current_url = next_url
                                continue
                            if response.status >= 400:
                                raise ExtractError(
                                    code="upstream_http_error",
                                    message=f"Target returned HTTP {response.status}.",
                                    status_code=502,
                                    retryable=response.status == 429 or response.status >= 500,
                                )
                            body = bytearray()
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                body.extend(chunk)
                                if len(body) > self._max_download_bytes:
                                    raise ExtractError(
                                        code="response_too_large",
                                        message="Target response exceeds the download limit.",
                                        status_code=413,
                                        retryable=False,
                                    )
                            return FetchResult(
                                requested_url=requested_url,
                                final_url=str(response.url),
                                status_code=response.status,
                                content_type=response.headers.get(
                                    "Content-Type", "application/octet-stream"
                                ),
                                charset=response.charset,
                                body=bytes(body),
                            )
                    except ExtractError:
                        raise
                    except TimeoutError as exc:
                        raise ExtractError(
                            code="fetch_timeout",
                            message="Target did not respond before the timeout.",
                            status_code=504,
                            retryable=True,
                        ) from exc
                    except aiohttp.ClientError as exc:
                        raise ExtractError(
                            code="target_unreachable",
                            message="Target could not be reached.",
                            status_code=502,
                            retryable=True,
                        ) from exc
        finally:
            await connector.close()
        raise AssertionError("redirect loop exited without a result")


__all__ = ["HttpFetcher", "PublicResolver"]
