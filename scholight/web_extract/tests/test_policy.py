from __future__ import annotations

import ipaddress

import pytest

from scholight.web_extract.errors import ExtractError
from scholight.web_extract.policy import ResolvedTarget, validate_public_target


@pytest.mark.asyncio
async def test_validate_public_target_accepts_public_arbitrary_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(_host: str, _port: int) -> tuple[ipaddress.IPv4Address, ...]:
        return (ipaddress.ip_address("93.184.216.34"),)

    monkeypatch.setattr("scholight.web_extract.policy._resolve_addresses", resolve)

    target = await validate_public_target("https://example.com:8443/path")

    assert target == ResolvedTarget(
        url="https://example.com:8443/path",
        host="example.com",
        port=8443,
        addresses=(ipaddress.ip_address("93.184.216.34"),),
    )


@pytest.mark.asyncio
async def test_validate_public_target_rejects_private_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(_host: str, _port: int) -> tuple[ipaddress.IPv4Address, ...]:
        return (ipaddress.ip_address("10.0.0.8"),)

    monkeypatch.setattr("scholight.web_extract.policy._resolve_addresses", resolve)

    with pytest.raises(ExtractError, match="public") as exc_info:
        await validate_public_target("http://internal.example:9000")

    assert exc_info.value.code == "private_target"


@pytest.mark.asyncio
async def test_validate_public_target_rejects_ipv4_mapped_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def resolve(_host: str, _port: int) -> tuple[ipaddress.IPv6Address, ...]:
        return (ipaddress.ip_address("::ffff:127.0.0.1"),)

    monkeypatch.setattr("scholight.web_extract.policy._resolve_addresses", resolve)

    with pytest.raises(ExtractError) as exc_info:
        await validate_public_target("https://mapped.example")

    assert exc_info.value.code == "private_target"


@pytest.mark.asyncio
async def test_validate_public_target_rejects_non_http_scheme() -> None:
    with pytest.raises(ExtractError) as exc_info:
        await validate_public_target("file:///etc/passwd")

    assert exc_info.value.code == "unsupported_scheme"


@pytest.mark.asyncio
async def test_validate_public_target_rejects_url_credentials() -> None:
    with pytest.raises(ExtractError) as exc_info:
        await validate_public_target("https://user:password@example.com")

    assert exc_info.value.code == "invalid_url"
