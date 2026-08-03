"""Network target validation for public Web Extract requests."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from scholight.web_extract.errors import ExtractError

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
_DNS_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    url: str
    host: str
    port: int
    addresses: tuple[IPAddress, ...]


async def _resolve_addresses(host: str, port: int) -> tuple[IPAddress, ...]:
    try:
        records = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                host,
                port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            ),
            timeout=_DNS_TIMEOUT_SECONDS,
        )
    except (TimeoutError, socket.gaierror) as exc:
        raise ExtractError(
            code="dns_failed",
            message="Target hostname could not be resolved.",
            status_code=502,
            retryable=True,
        ) from exc
    unique = {ipaddress.ip_address(record[4][0]) for record in records}
    if not unique:
        raise ExtractError(
            code="dns_failed",
            message="Target hostname resolved without usable addresses.",
            status_code=502,
            retryable=True,
        )
    return tuple(sorted(unique, key=lambda address: (address.version, int(address))))


def _is_public(address: IPAddress) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global


async def resolve_public_addresses(host: str, port: int) -> tuple[IPAddress, ...]:
    addresses = await _resolve_addresses(host, port)
    if any(not _is_public(address) for address in addresses):
        raise ExtractError(
            code="private_target",
            message="Target must resolve only to public network addresses.",
            status_code=403,
            retryable=False,
        )
    return addresses


async def validate_public_target(url: str) -> ResolvedTarget:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ExtractError(
            code="invalid_url",
            message="URL is invalid.",
            status_code=400,
            retryable=False,
        ) from exc
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ExtractError(
            code="unsupported_scheme",
            message="Only HTTP and HTTPS URLs are supported.",
            status_code=400,
            retryable=False,
        )
    if parsed.hostname is None:
        raise ExtractError(
            code="invalid_url",
            message="URL must include a hostname.",
            status_code=400,
            retryable=False,
        )
    if parsed.username is not None or parsed.password is not None:
        raise ExtractError(
            code="invalid_url",
            message="URL credentials are not supported; use target headers instead.",
            status_code=400,
            retryable=False,
        )
    host = parsed.hostname.rstrip(".").lower()
    effective_port = port or (443 if scheme == "https" else 80)
    addresses = await resolve_public_addresses(host, effective_port)
    normalized = urlunsplit((scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))
    return ResolvedTarget(
        url=normalized,
        host=host,
        port=effective_port,
        addresses=addresses,
    )


__all__ = ["ResolvedTarget", "resolve_public_addresses", "validate_public_target"]
