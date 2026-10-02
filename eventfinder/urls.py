"""Centralized public-URL validation for discovery and outbound presentation."""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

import httpx


class UnsafeURL(ValueError):
    pass


Resolver = Callable[[str], Awaitable[list[str]]]

# Query parameters that only carry marketing/analytics provenance never a
# distinct destination or fact; stripping them lets cross-posted links that
# differ solely by tracking params dedupe to the same canonical event.
_TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "mc_eid", "igshid"})
_MEETUP_TRACKING_QUERY_KEYS = frozenset({"recid", "recsource", "searchid", "eventorigin"})
_MEETUP_EVENT_PATH = re.compile(r"^/(?:[^/]+/)?events/[^/]+/?$")
_NVIDIA_WEBINAR_ROUTE = re.compile(r"/webinar/[0-9]+")


def _event_route_fragment(parsed: SplitResult) -> str:
    """Keep the published NVIDIA portal's event route, not ordinary anchors."""

    if (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").casefold() == "www.nvidia.com"
        and parsed.port in {None, 443}
        and parsed.path == "/en-us/about-nvidia/webinar-portal/"
        and _NVIDIA_WEBINAR_ROUTE.fullmatch(parsed.fragment)
    ):
        return parsed.fragment
    return ""


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
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not _is_public_ip(str(literal)):
            raise UnsafeURL("non-public IP URL is not allowed")
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, _event_route_fragment(parsed)))


def normalize_url(url: str) -> str:
    """Return a syntax-validated URL with a case-folded host and tracking
    query parameters removed.

    This never widens what ``validate_url_syntax`` accepts; it only makes two
    observationally-equivalent public URLs (differing solely by host case or
    marketing/analytics query params) compare equal so cross-postings dedupe.
    """

    validated = validate_url_syntax(url)
    parsed = urlsplit(validated)
    hostname = (parsed.hostname or "").casefold()
    netloc = f"[{hostname}]" if ":" in hostname else hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    kept_params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    return urlunsplit((parsed.scheme, netloc, parsed.path or "/", urlencode(kept_params), _event_route_fragment(parsed)))


def meetup_event_identity_url(url: str) -> str | None:
    """Compare Meetup event URLs without recommendation query parameters.

    This is an identity key only. The observed canonical and source URLs stay
    intact for outbound links and provenance.
    """

    try:
        parsed = urlsplit(normalize_url(url))
    except UnsafeURL:
        return None
    if parsed.hostname not in {"meetup.com", "www.meetup.com"} or not _MEETUP_EVENT_PATH.fullmatch(parsed.path):
        return None
    kept_params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.casefold() not in _MEETUP_TRACKING_QUERY_KEYS
    ]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), urlencode(kept_params), ""))


def nvidia_webinar_identity_url(url: str) -> str | None:
    """Return an identity only for the portal's published numeric event route."""

    try:
        normalized = normalize_url(url)
    except UnsafeURL:
        return None
    return normalized if _event_route_fragment(urlsplit(normalized)) else None


def event_identity_url(url: str) -> str:
    """Return a conservative identity key for same-fetch event deduplication."""

    if meetup_identity := meetup_event_identity_url(url):
        return meetup_identity
    try:
        return normalize_url(url)
    except UnsafeURL:
        # Parsing can precede URLSafety's explicit rejection at the fetch
        # boundary; identity comparison must not make one bad item fail a page.
        return url


def same_event_destination(first: str | None, second: str | None) -> bool:
    """Match two present event links after only known tracking is removed."""

    return bool(first and second) and event_identity_url(first) == event_identity_url(second)


async def default_resolver(hostname: str) -> list[str]:
    loop = asyncio.get_running_loop()
    results = await loop.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    return sorted({item[4][0] for item in results})


async def _resolve_public_ip(hostname: str, resolver: Resolver) -> str:
    """Resolve (or accept as a literal) a hostname and return one validated
    public IP address, failing closed on any private/failed/empty result."""

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        pass
    else:
        if not _is_public_ip(str(literal)):
            raise UnsafeURL("non-public IP URL is not allowed")
        return str(literal)
    try:
        addresses = await resolver(hostname)
    except OSError as error:
        raise UnsafeURL("DNS resolution failed") from error
    if not addresses:
        raise UnsafeURL("DNS returned no addresses") from None
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as error:
            raise UnsafeURL("DNS returned an invalid address") from error
        if not _is_public_ip(str(parsed_address)):
            raise UnsafeURL("DNS resolved to a non-public address") from None
    return addresses[0]


class URLSafety:
    """DNS-aware SSRF protection with an injectable resolver for offline tests."""

    def __init__(self, resolver: Resolver | None = None):
        self.resolver = resolver or default_resolver

    async def validate(self, url: str) -> str:
        normalized = validate_url_syntax(url)
        hostname = urlsplit(normalized).hostname
        assert hostname is not None
        await _resolve_public_ip(hostname, self.resolver)
        return normalized


class URLSafeTransport(httpx.AsyncBaseTransport):
    """DNS-pins every connection to a validated public IP, closing the
    rebinding TOCTOU between the pre-connect ``URLSafety.validate`` check and
    the transport's own DNS lookup at connect time.

    A fresh request is built for the inner transport with the host rewritten
    to the validated IP; the original request object (and therefore
    ``response.url``/provenance seen by callers) is left untouched, while the
    ``Host`` header and TLS SNI are pinned to the original hostname so the
    origin server still sees the expected virtual host.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ):
        self.inner = inner or httpx.AsyncHTTPTransport()
        self.resolver = resolver or default_resolver

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        original_host = request.url.host
        pinned_ip = await _resolve_public_ip(original_host, self.resolver)
        pinned_request = httpx.Request(
            method=request.method,
            url=request.url.copy_with(host=pinned_ip),
            headers=request.headers,
            stream=request.stream,
            extensions=dict(request.extensions),
        )
        # httpx.URL.host excludes any port, so a source URL with an explicit
        # non-default port would otherwise send a Host header missing
        # ":port". Reconstruct the host(:port) httpx would itself have
        # generated for the original URL; SNI stays hostname-only.
        original_port = request.url.port
        host_header = f"[{original_host}]" if ":" in original_host else original_host
        pinned_request.headers["Host"] = (
            f"{host_header}:{original_port}" if original_port else host_header
        )
        pinned_request.extensions["sni_hostname"] = original_host
        return await self.inner.handle_async_request(pinned_request)

    async def aclose(self) -> None:
        await self.inner.aclose()


def make_public_fetch_client(timeout: float | httpx.Timeout = 20.0) -> httpx.AsyncClient:
    """Create a DNS-pinned client without cross-origin connection reuse."""

    transport = URLSafeTransport(
        inner=httpx.AsyncHTTPTransport(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=0)
        )
    )
    return httpx.AsyncClient(transport=transport, timeout=timeout)


def safe_outbound_url(url: str | None) -> str | None:
    """Presentation guard; discovered URLs have already passed DNS validation."""

    if not url:
        return None
    try:
        return normalize_url(url)
    except UnsafeURL:
        return None
