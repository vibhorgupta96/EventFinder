"""Centralized public-URL validation for discovery and outbound presentation."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit


class UnsafeURL(ValueError):
    pass


Resolver = Callable[[str], Awaitable[list[str]]]


def _is_public_ip(value: str) -> bool:
    address = ipaddress.ip_address(value)
    # ``is_global`` also excludes carrier-grade NAT and other non-public
    # special-purpose ranges not covered by the individual flags alone.
    return address.is_global and not any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_reserved,
            address.is_unspecified,
            address.is_multicast,
        )
    )


def validate_url_syntax(url: str) -> str:
    """Return a normalized HTTP(S) URL without credentials or local targets."""

    try:
        parsed = urlsplit(url.strip())
        _ = parsed.port
    except (AttributeError, ValueError) as error:
        raise UnsafeURL("invalid URL") from error
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeURL("only HTTP(S) URLs are allowed")
    if not parsed.hostname or parsed.username or parsed.password:
        raise UnsafeURL("URL must have a hostname and no credentials")
    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise UnsafeURL("localhost URLs are not allowed")
    try:
        if not _is_public_ip(hostname):
            raise UnsafeURL("non-public IP URL is not allowed")
    except ValueError:
        pass
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))


async def default_resolver(hostname: str) -> list[str]:
    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return sorted({item[4][0] for item in results})


class URLSafety:
    """DNS-aware SSRF protection with an injectable resolver for offline tests."""

    def __init__(self, resolver: Resolver | None = None):
        self.resolver = resolver or default_resolver

    async def validate(self, url: str) -> str:
        normalized = validate_url_syntax(url)
        hostname = urlsplit(normalized).hostname
        assert hostname is not None
        try:
            literal = ipaddress.ip_address(hostname)
        except ValueError:
            try:
                addresses = await self.resolver(hostname)
            except OSError as error:
                raise UnsafeURL("DNS resolution failed") from error
            if not addresses:
                raise UnsafeURL("DNS returned no addresses") from None
            for address in addresses:
                try:
                    if not _is_public_ip(address):
                        raise UnsafeURL("DNS resolved to a non-public address") from None
                except ValueError as error:
                    raise UnsafeURL("DNS returned an invalid address") from error
        else:
            if not _is_public_ip(str(literal)):
                raise UnsafeURL("non-public IP URL is not allowed")
        return normalized


def safe_outbound_url(url: str | None) -> str | None:
    """Presentation guard; discovered URLs have already passed DNS validation."""

    if not url:
        return None
    try:
        return validate_url_syntax(url)
    except UnsafeURL:
        return None
