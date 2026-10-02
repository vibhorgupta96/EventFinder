from __future__ import annotations

import httpx
import pytest
from eventfinder.urls import (
    UnsafeURL,
    URLSafeTransport,
    URLSafety,
    make_public_fetch_client,
    normalize_url,
    nvidia_webinar_identity_url,
    safe_outbound_url,
    same_event_destination,
    validate_url_syntax,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Example.COM/path", "https://example.com/path"),
        ("https://EXAMPLE.com", "https://example.com/"),
        ("https://Example.COM:8443/x", "https://example.com:8443/x"),
        ("https://[2606:4700:4700::1111]/x", "https://[2606:4700:4700::1111]/x"),
        ("https://[2606:4700:4700::1111]:8443/x", "https://[2606:4700:4700::1111]:8443/x"),
        (
            "https://example.com/e?utm_source=fb&id=42&utm_campaign=x",
            "https://example.com/e?id=42",
        ),
        ("https://example.com/e?fbclid=abc&ref=y", "https://example.com/e?ref=y"),
        (
            "https://example.com/e?gclid=abc&mc_eid=q&igshid=z&keep=1",
            "https://example.com/e?keep=1",
        ),
        ("https://example.com/e?UTM_Source=fb&id=1", "https://example.com/e?id=1"),
    ],
)
def test_normalize_url_lowercases_host_and_strips_tracking_params(url, expected):
    assert normalize_url(url) == expected


def test_normalize_url_preserves_order_of_surviving_query_params():
    normalized = normalize_url("https://example.com/e?b=2&utm_source=x&a=1&gclid=y&c=3")
    assert normalized == "https://example.com/e?b=2&a=1&c=3"


def test_nvidia_published_hash_routes_remain_distinct_during_validation_and_presentation():
    first = "https://www.nvidia.com/en-us/about-nvidia/webinar-portal/#/webinar/5503388"
    second = "https://www.nvidia.com/en-us/about-nvidia/webinar-portal/#/webinar/5503271"
    assert validate_url_syntax(first) == first
    assert normalize_url(first) == first
    assert nvidia_webinar_identity_url(first) == first
    assert safe_outbound_url(second) == second
    assert same_event_destination(first, second) is False
    assert normalize_url(first.replace("/#", "/?utm_source=calendar#")) == first


@pytest.mark.parametrize(
    "url",
    [
        "https://www.nvidia.com/en-us/about-nvidia/webinar-portal/#/webinar/not-numeric",
        "https://www.nvidia.com/en-us/about-nvidia/webinar-portal/#/webinar/1/extra",
        "https://www.nvidia.com/en-us/about-nvidia/webinar-portal/#/webinar/1?other=2",
        "https://www.nvidia.com/en-us/about-nvidia/webinar-portal/#section",
        "https://www.nvidia.com/en-us/other/#/webinar/1",
        "https://nvidia.com/en-us/about-nvidia/webinar-portal/#/webinar/1",
        "https://example.com/en-us/about-nvidia/webinar-portal/#/webinar/1",
    ],
)
def test_only_the_published_numeric_nvidia_portal_route_keeps_a_fragment(url):
    assert "#" not in validate_url_syntax(url)
    assert "#" not in normalize_url(url)
    assert nvidia_webinar_identity_url(url) is None


def test_normalize_url_rejects_unsafe_urls_like_validate_url_syntax():
    with pytest.raises(UnsafeURL):
        normalize_url("javascript:alert(1)")
    with pytest.raises(UnsafeURL):
        normalize_url("http://localhost/x")


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/x", "http://10.0.0.5/x", "https://100.64.1.31/x", "http://[::1]/x", "http://[fc00::1]/x"],
)
def test_private_ip_literals_fail_syntax_normalization_and_presentation(url):
    with pytest.raises(UnsafeURL, match="non-public IP"):
        validate_url_syntax(url)
    with pytest.raises(UnsafeURL, match="non-public IP"):
        normalize_url(url)
    assert safe_outbound_url(url) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("addresses", "reason"),
    [
        (["100.64.1.31"], "DNS resolved to a non-public address"),
        (["10.0.0.5"], "DNS resolved to a non-public address"),
        (["not-an-address"], "DNS returned an invalid address"),
    ],
)
async def test_dns_rejections_keep_the_actual_failure_reason(addresses, reason):
    async def resolver(_hostname):
        return addresses

    with pytest.raises(UnsafeURL, match=reason):
        await URLSafety(resolver).validate("https://events.example.test/listing")


@pytest.mark.asyncio
async def test_public_fetch_factory_pins_transport_and_disables_keepalive(monkeypatch):
    seen: dict = {}
    inner = _StubInnerTransport()

    def transport_factory(*, limits):
        seen["limits"] = limits
        return inner

    monkeypatch.setattr("eventfinder.urls.httpx.AsyncHTTPTransport", transport_factory)
    monkeypatch.setattr("eventfinder.urls.default_resolver", _public_resolver)
    timeout = httpx.Timeout(7.5)
    async with make_public_fetch_client(timeout=timeout) as client:
        assert isinstance(client._transport, URLSafeTransport)
        assert client.timeout == timeout
        response = await client.get("https://events.example.test/listing")

    assert response.status_code == 200
    assert inner.requests[0].url.host == "93.184.216.34"
    assert seen["limits"].max_connections == 20
    assert seen["limits"].max_keepalive_connections == 0


class _StubInnerTransport(httpx.AsyncBaseTransport):
    """Records exactly what would have been sent over the wire."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, text="ok")


async def _public_resolver(_hostname: str) -> list[str]:
    return ["93.184.216.34"]


async def _private_resolver(_hostname: str) -> list[str]:
    return ["10.0.0.5"]


async def _public_then_private_resolver(_hostname: str) -> list[str]:
    return ["93.184.216.34", "192.168.1.1"]


@pytest.mark.asyncio
async def test_url_safe_transport_pins_resolved_public_ip_and_preserves_host():
    inner = _StubInnerTransport()
    transport = URLSafeTransport(inner=inner, resolver=_public_resolver)
    request = httpx.Request("GET", "https://events.example.test/listing")

    response = await transport.handle_async_request(request)

    assert response.status_code == 200
    assert len(inner.requests) == 1
    sent = inner.requests[0]
    assert sent.url.host == "93.184.216.34"
    assert sent.headers["Host"] == "events.example.test"
    assert sent.extensions["sni_hostname"] == "events.example.test"
    # The caller's own request object is left untouched: downstream
    # response.url/provenance must still reflect the real hostname.
    assert request.url.host == "events.example.test"


@pytest.mark.asyncio
async def test_url_safe_transport_pins_public_ip_literal_destination_directly():
    inner = _StubInnerTransport()
    transport = URLSafeTransport(inner=inner, resolver=_public_resolver)
    request = httpx.Request("GET", "https://93.184.216.34/listing")

    await transport.handle_async_request(request)

    assert inner.requests[0].url.host == "93.184.216.34"
    assert inner.requests[0].headers["Host"] == "93.184.216.34"


@pytest.mark.asyncio
async def test_url_safe_transport_rejects_private_dns_result_without_calling_inner():
    inner = _StubInnerTransport()
    transport = URLSafeTransport(inner=inner, resolver=_private_resolver)
    request = httpx.Request("GET", "https://events.example.test/listing")

    with pytest.raises(UnsafeURL):
        await transport.handle_async_request(request)
    assert inner.requests == []


@pytest.mark.asyncio
async def test_url_safe_transport_rejects_if_any_resolved_address_is_private():
    inner = _StubInnerTransport()
    transport = URLSafeTransport(inner=inner, resolver=_public_then_private_resolver)
    request = httpx.Request("GET", "https://events.example.test/listing")

    with pytest.raises(UnsafeURL):
        await transport.handle_async_request(request)
    assert inner.requests == []


@pytest.mark.asyncio
async def test_url_safe_transport_rejects_private_ip_literal_without_calling_inner():
    inner = _StubInnerTransport()
    transport = URLSafeTransport(inner=inner, resolver=_public_resolver)
    request = httpx.Request("GET", "http://127.0.0.1/admin")

    with pytest.raises(UnsafeURL):
        await transport.handle_async_request(request)
    assert inner.requests == []


@pytest.mark.asyncio
async def test_url_safe_transport_preserves_explicit_non_default_port_in_host_header():
    """httpx.URL.host excludes any port, so a source URL with an explicit
    non-default port must still produce a Host header that includes the
    port, while the connection is pinned to the resolved IP and SNI stays
    hostname-only (no port)."""
    inner = _StubInnerTransport()
    transport = URLSafeTransport(inner=inner, resolver=_public_resolver)
    request = httpx.Request("GET", "https://events.example.test:8443/x")

    await transport.handle_async_request(request)

    sent = inner.requests[0]
    assert sent.url.host == "93.184.216.34"
    assert sent.headers["Host"] == "events.example.test:8443"
    assert sent.extensions["sni_hostname"] == "events.example.test"


@pytest.mark.asyncio
async def test_url_safe_transport_preserves_ipv6_brackets_in_host_header():
    inner = _StubInnerTransport()
    transport = URLSafeTransport(inner=inner, resolver=_public_resolver)
    await transport.handle_async_request(httpx.Request("GET", "https://[2606:4700:4700::1111]:8443/x"))
    assert inner.requests[0].headers["Host"] == "[2606:4700:4700::1111]:8443"


@pytest.mark.asyncio
async def test_url_safe_transport_via_async_client_reaches_mock_transport_pinned():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "93.184.216.34"
        assert request.headers["Host"] == "events.example.test"
        return httpx.Response(200, text="ok")

    transport = URLSafeTransport(inner=httpx.MockTransport(handler), resolver=_public_resolver)
    async with httpx.AsyncClient(transport=transport) as client:
        response = await client.get("https://events.example.test/listing")

    assert response.status_code == 200
    # response.url must reflect the ORIGINAL hostname, not the pinned IP, so
    # downstream evidence/relative-link joining is unaffected by the pin.
    assert str(response.url) == "https://events.example.test/listing"
