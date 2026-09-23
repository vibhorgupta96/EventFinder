from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from eventfinder.config import SourceDefinition
from eventfinder.policy import assess_candidate
from eventfinder.sources import (
    PublicPageEventSource,
    RequestLimiter,
    RobotsPolicy,
    SourceFetchError,
    make_source,
    parse_event_page,
)
from eventfinder.urls import UnsafeURL, URLSafety, validate_url_syntax


async def public_resolver(_hostname: str) -> list[str]:
    return ["93.184.216.34"]


def _safety() -> URLSafety:
    return URLSafety(public_resolver)


def test_json_ld_parser_extracts_event_fixture():
    content = Path("tests/fixtures/event_page.html").read_text(encoding="utf-8")
    candidates = parse_event_page(content, "https://community.example.test/list", "fixture", datetime.now(UTC))
    assert len(candidates) == 1
    event = candidates[0]
    assert event.title == "Bengaluru AI Systems Meetup"
    assert event.city == "Bangalore"
    assert event.starts_at.tzinfo == UTC
    assert event.speakers == ["Ada Engineer"]
    assert event.is_explicitly_paid is False


def test_malformed_json_ld_is_ignored_and_opengraph_falls_back():
    html = """<meta property='og:title' content='Online AI Workshop'><meta property='og:description' content='Register for technical AI session'><script type='application/ld+json'>{bad}</script>"""
    candidates = parse_event_page(html, "https://example.test/event", "fixture")
    assert candidates[0].title == "Online AI Workshop"
    assert candidates[0].registration_state.value == "open"


def test_timezone_less_source_time_defaults_to_ist_then_stores_utc():
    html = """<script type='application/ld+json'>{"@type":"Event","name":"AI meetup","startDate":"2026-10-10T18:30:00","location":{"name":"Bengaluru"}}</script>"""
    event = parse_event_page(html, "https://example.test/event", "fixture")[0]
    assert event.starts_at.isoformat() == "2026-10-10T13:00:00+00:00"


@pytest.mark.parametrize(
    ("event_status", "availability", "expected"),
    [
        ("https://schema.org/EventCancelled", None, "cancelled"),
        ("https://schema.org/EventPostponed", None, "postponed"),
        (None, "https://schema.org/SoldOut", "sold_out"),
        (None, "https://schema.org/Discontinued", "closed"),
        (None, "https://schema.org/InStock", "open"),
    ],
)
def test_schema_lifecycle_and_offer_facts_are_extracted(event_status, availability, expected):
    event_status_json = f', "eventStatus": "{event_status}"' if event_status else ""
    availability_json = f', "availability": "{availability}"' if availability else ""
    html = f"""<script type='application/ld+json'>{{
      "@type":"Event", "name":"AI Workshop", "startDate":"2026-10-10T10:00:00+05:30"{event_status_json},
      "offers":{{"price":"0", "priceCurrency":"INR", "validFrom":"2026-09-20T10:00:00+05:30"{availability_json}}}
    }}</script>"""
    candidate = parse_event_page(html, "https://events.example.test/schema", "fixture")[0]
    assert candidate.registration_state.value == expected
    assert candidate.registration_opened_at == datetime(2026, 9, 20, 4, 30, tzinfo=UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("platform", "price", "is_paid"),
    [
        ("luma", "Paid", True),
        ("meetup", "Paid admission", True),
        ("hasgeek", "₹499 onwards", True),
        ("devfolio", "INR 499–999", True),
        ("unstop", "From ₹499", True),
        ("eventbrite", "Free", False),
        ("official", "0", False),
        ("official", "Nada", False),
    ],
)
async def test_semantic_price_evidence_marks_paid_events_for_policy(
    platform, price, is_paid, config, organizers
):
    html = f"""<main><h1>AI Workshop</h1><dl>
    <dt>Starts</dt><dd>2026-10-10 10:00 IST</dd>
    <dt>Venue</dt><dd>Bengaluru</dd><dt>Cost</dt><dd>{price}</dd>
    <dt>Registration</dt><dd>Open</dd></dl><a href="/register">Register</a></main>"""
    candidate = parse_event_page(
        html,
        f"https://{platform}.example.test/event",
        f"{platform}_fixture",
        datetime(2026, 9, 23, tzinfo=UTC),
        platform,
    )[0]
    assert candidate.is_explicitly_paid is is_paid
    assessment = await assess_candidate(candidate, config, organizers)
    assert assessment.status == ("rejected" if is_paid else "eligible")


@pytest.mark.asyncio
async def test_public_page_adapter_respects_robots_and_parses():
    fixture = Path("tests/fixtures/event_page.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text=fixture)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicPageEventSource(
            SourceDefinition(name="fixture", adapter="public_page", url="https://example.test/events"),
            client,
            RobotsPolicy(client, _safety()),
            _safety(),
        )
        result = await source.fetch()
    assert len(result.candidates) == 1
    assert result.source_evidence[0].facts["candidate_count"] == 1


@pytest.mark.asyncio
async def test_rate_limit_marks_source_unhealthy_without_retrying():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(429)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = make_source(
            SourceDefinition(name="fixture", adapter="public_page", url="https://example.test/events"),
            client,
            safety=_safety(),
        )
        with pytest.raises(SourceFetchError, match="rate limited") as error:
            await source.fetch()
    assert error.value.status_code == 429


def test_factory_covers_both_adapter_contracts():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="User-agent: *\nAllow: /"))
    # Factory dispatch is synchronous even though fetch itself is async.
    client = httpx.AsyncClient(transport=transport)
    try:
        assert make_source(SourceDefinition(name="page", adapter="public_page", url="https://x.test"), client).__class__.__name__ == "PublicPageEventSource"
        assert make_source(SourceDefinition(name="search", adapter="search", query="test"), client).__class__.__name__ == "SearchEventSource"
    finally:
        asyncio.run(client.aclose())


@pytest.mark.parametrize(
    "platform",
    ["luma", "meetup", "hasgeek", "devfolio", "unstop", "eventbrite", "official"],
)
def test_seeded_platform_documented_html_extracts_registration_evidence(platform):
    fixture = Path("tests/fixtures/platform_event.html").read_text(encoding="utf-8")
    candidate = parse_event_page(
        fixture,
        "https://events.example.test/listing",
        f"{platform}_fixture",
        datetime(2026, 9, 23, tzinfo=UTC),
        platform,
    )[0]
    assert candidate.canonical_url == "https://events.example.test/platform-fixture"
    assert candidate.registration_url == "https://events.example.test/platform-fixture/register"
    assert candidate.registration_state.value == "open"
    assert candidate.registration_opened_at == datetime(2026, 9, 20, 3, tzinfo=UTC)
    assert candidate.registration_deadline == datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert candidate.eligibility_text == "Open to practitioners"
    assert candidate.price_text == "Free"


@pytest.mark.parametrize(
    ("platform", "fixture_name", "expected_state"),
    [
        ("luma", "luma_event.html", "open"),
        ("meetup", "meetup_event.html", "unknown"),
        ("hasgeek", "hasgeek_event.html", "open"),
        ("devfolio", "devfolio_event.html", "open"),
        ("unstop", "unstop_event.html", "closed"),
        ("eventbrite", "eventbrite_event.html", "open"),
        ("official", "microsoft_reactor_event.html", "open"),
    ],
)
def test_platform_semantic_label_fixtures_extract_only_evidenced_fields(
    platform, fixture_name, expected_state
):
    html = Path("tests/fixtures", fixture_name).read_text(encoding="utf-8")
    candidate = parse_event_page(
        html,
        f"https://{platform}.example.test/event",
        f"{platform}_fixture",
        datetime(2026, 9, 23, tzinfo=UTC),
        platform,
    )[0]
    assert candidate.evidence.facts["parser"] == f"{platform}:semantic_labels"
    assert candidate.starts_at is not None
    assert candidate.registration_state.value == expected_state
    assert candidate.registration_url.startswith("https://")
    if platform == "luma":
        assert candidate.registration_opened_at == datetime(2026, 9, 22, 3, 30, tzinfo=UTC)
        assert candidate.organizer == "Google Developer Groups Bengaluru"
    if platform == "meetup":
        assert candidate.registration_url == "https://tickets.example.test/cloud-native"
    if platform == "hasgeek":
        assert candidate.registration_deadline == datetime(2026, 10, 11, 12, 30, tzinfo=UTC)
        assert candidate.format.value == "in_person"
    if platform == "devfolio":
        assert candidate.price_text == "Free"
    if platform == "unstop":
        assert candidate.eligibility_text == "Open to practitioners"
        assert candidate.price_text == "Free"
    if platform == "eventbrite":
        assert candidate.registration_deadline == datetime(2026, 10, 14, 11, 30, tzinfo=UTC)
    if platform == "official":
        assert candidate.format.value == "online"
        assert candidate.speakers == ["Ada Engineer"]


@pytest.mark.asyncio
async def test_search_results_are_hydrated_before_becoming_candidates(monkeypatch):
    fixture = Path("tests/fixtures/platform_event.html").read_text(encoding="utf-8")

    class Search:
        def text(self, *_args, **_kwargs):
            return [{"href": "https://events.example.test/platform-fixture", "body": "untrusted snippet"}]

    monkeypatch.setattr("eventfinder.sources.DDGS", Search)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text=fixture)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = make_source(
            SourceDefinition(
                name="search",
                adapter="search",
                query="technical events",
                platform="eventbrite",
                allowed_domains=["events.example.test"],
            ),
            client,
            RobotsPolicy(client, _safety()),
            _safety(),
        )
        result = await source.fetch()
    assert result.candidates[0].starts_at == datetime(2026, 10, 2, 12, tzinfo=UTC)
    assert result.candidates[0].registration_deadline == datetime(2026, 10, 1, 12, tzinfo=UTC)
    assert result.source_evidence[0].facts["parser"] == "search_hydration"


@pytest.mark.asyncio
async def test_robots_is_cached_per_origin_and_checked_per_path():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="User-agent: *\nDisallow: /private")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        policy = RobotsPolicy(client, _safety())
        assert await policy.allows("https://events.example.test/public")
        assert not await policy.allows("https://events.example.test/private/event")
    assert calls == ["/robots.txt"]


@pytest.mark.asyncio
async def test_missing_robots_file_allows_public_page_by_default():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(404))
    ) as client:
        assert await RobotsPolicy(client, _safety()).allows("https://events.example.test/event")


@pytest.mark.asyncio
async def test_bounded_listing_pagination_only_fetches_configured_pages():
    fixture = Path("tests/fixtures/platform_event.html").read_text(encoding="utf-8")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text=fixture)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicPageEventSource(
            SourceDefinition(
                name="fixture",
                adapter="public_page",
                platform="official",
                url="https://events.example.test/listing",
                max_pages=2,
                pagination_param="page",
            ),
            client,
            RobotsPolicy(client, _safety()),
            _safety(),
        )
        result = await source.fetch()
    assert len(result.candidates) == 1
    assert requested == [
        "https://events.example.test/robots.txt",
        "https://events.example.test/listing",
        "https://events.example.test/listing?page=2",
    ]


@pytest.mark.asyncio
async def test_per_origin_rate_limit_applies_to_each_paginated_request():
    fixture = Path("tests/fixtures/platform_event.html").read_text(encoding="utf-8")
    elapsed = [0.0]
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        elapsed[0] += delay

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text=fixture)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicPageEventSource(
            SourceDefinition(
                name="fixture",
                adapter="public_page",
                platform="official",
                url="https://events.example.test/listing",
                max_pages=2,
                pagination_param="page",
                rate_limit_seconds=3,
            ),
            client,
            RobotsPolicy(client, _safety()),
            _safety(),
            RequestLimiter(sleeper=sleep, clock=lambda: elapsed[0]),
        )
        await source.fetch()
    assert delays == [3]


@pytest.mark.asyncio
async def test_redirect_outside_source_allowed_domains_is_rejected():
    from eventfinder.sources import _request_redirect_checked

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(302, headers={"location": "https://outside.example.test/event"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceFetchError, match="allowed domains"):
            await _request_redirect_checked(
                client,
                "https://events.example.test/redirect",
                _safety(),
                allowed_domains={"events.example.test"},
            )


@pytest.mark.asyncio
async def test_redirect_hop_and_outbound_urls_cannot_target_private_networks():
    from eventfinder.sources import _request_redirect_checked

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(UnsafeURL):
            await _request_redirect_checked(client, "https://events.example.test/redirect", _safety())
    for value in ("javascript:alert(1)", "data:text/html,hi", "http://localhost/x", "https://u:p@example.test/x"):
        with pytest.raises(UnsafeURL):
            validate_url_syntax(value)
