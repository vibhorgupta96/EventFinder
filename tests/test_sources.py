from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from eventfinder.config import SourceDefinition, get_sources_registry
from eventfinder.domain import has_explicit_paid_price, normalize_price
from eventfinder.policy import assess_candidate
from eventfinder.sources import (
    MAX_RESPONSE_BYTES,
    PublicPageEventSource,
    RequestLimiter,
    RobotsPolicy,
    SearchEventSource,
    SourceFetchError,
    _label_time,
    _request_redirect_checked,
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


def test_global_source_timezone_interprets_naive_time_in_its_own_zone_not_ist():
    html = """<script type='application/ld+json'>{"@type":"Event","name":"AWS re:Invent session","startDate":"2026-10-10T18:30:00"}</script>"""
    event = parse_event_page(
        html,
        "https://aws.example.test/event",
        "aws_events",
        tz="America/Los_Angeles",
    )[0]
    # Same naive local wall-clock time as the IST-default fixture above, but a
    # source configured for America/Los_Angeles must not be treated as IST.
    assert event.starts_at.isoformat() == "2026-10-11T01:30:00+00:00"


def test_explicit_offset_is_never_overridden_by_source_timezone():
    html = """<script type='application/ld+json'>{"@type":"Event","name":"AWS re:Invent session","startDate":"2026-10-10T18:30:00+05:30"}</script>"""
    event = parse_event_page(
        html,
        "https://aws.example.test/event",
        "aws_events",
        tz="America/Los_Angeles",
    )[0]
    assert event.starts_at.isoformat() == "2026-10-10T13:00:00+00:00"


def test_label_time_dayfirst_toggle_changes_ambiguous_numeric_date_parsing():
    dayfirst = _label_time("03/04/2026")
    monthfirst = _label_time("03/04/2026", dayfirst=False)
    assert dayfirst.isoformat() == "2026-04-02T18:30:00+00:00"
    assert monthfirst.isoformat() == "2026-03-03T18:30:00+00:00"
    assert dayfirst != monthfirst


@pytest.mark.parametrize("garbage", ["Coming soon 2026", "TBD"])
def test_label_time_fails_closed_on_garbage_without_a_real_month_or_day(garbage):
    assert _label_time(garbage) is None


def test_garbage_date_never_fabricates_a_starts_at_on_a_real_candidate():
    html = """<script type='application/ld+json'>{"@type":"Event","name":"AI Meetup TBD","startDate":"Coming soon 2026"}</script>"""
    event = parse_event_page(html, "https://example.test/event", "fixture")[0]
    assert event.starts_at is None


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
async def test_multiple_schema_offers_preserve_paid_tier_evidence_for_policy(config, organizers):
    html = """<script type='application/ld+json'>{
      "@type":"Event", "name":"Bengaluru AI Expo",
      "startDate":"2026-10-10T10:00:00+05:30",
      "location":{"name":"Bengaluru"},
      "offers":[
        {"price":"0", "priceCurrency":"INR"},
        {"price":"499", "priceCurrency":"INR"}
      ]
    }</script>"""
    candidate = parse_event_page(html, "https://events.example.test/expo", "fixture")[0]
    assert candidate.price_text == "INR 0; INR 499"
    assert candidate.is_explicitly_paid is True
    assert (await assess_candidate(candidate, config, organizers)).status == "rejected"


@pytest.mark.parametrize(
    ("value", "normalized", "is_paid"),
    [
        (499, "499", True),
        (499.5, "499.5", True),
        (0, "0", False),
        (float("inf"), None, False),
        (" 499 INR ", "499 INR", True),
        ("Free entry; 499 INR workshop", "Free entry; 499 INR workshop", True),
    ],
)
def test_price_normalizer_handles_finite_numbers_postfix_currency_and_mixed_text(
    value, normalized, is_paid
):
    assert normalize_price(value) == normalized
    assert has_explicit_paid_price(value) is is_paid


@pytest.mark.parametrize(
    ("node", "expected_price", "is_paid"),
    [
        ('"price":499', "499", True),
        (
            '"offers":[{"price":0,"priceCurrency":"INR"},{"price":499,"priceCurrency":"INR"}]',
            "INR 0; INR 499",
            True,
        ),
        ('"price":"499 INR"', "499 INR", True),
    ],
)
def test_schema_numeric_and_postfix_prices_are_preserved(node, expected_price, is_paid):
    html = f"""<script type='application/ld+json'>{{
      "@type":"Event", "name":"Bengaluru AI Workshop",
      "startDate":"2026-10-10T10:00:00+05:30", "location":{{"name":"Bengaluru"}},
      {node}
    }}</script>"""
    candidate = parse_event_page(html, "https://events.example.test/price", "fixture")[0]
    assert candidate.price_text == expected_price
    assert candidate.is_explicitly_paid is is_paid


@pytest.mark.parametrize(
    ("source_name", "fixture_name", "page_url", "expected_organizer"),
    [
        ("foss_united_bengaluru", "foss_united_event.html", "https://platform.fossunited.org/c/bengaluru/april-meetup", "FOSS United"),
        ("global_ai_bengaluru", "global_ai_event.html", "https://globalai.community/e/bd1o37ln", "Global AI Bengaluru"),
        ("cncf_bengaluru", "cncf_bengaluru_event.html", "https://ocgroups.dev/cncf/group/52r68y4/event/abcd", "CNCF"),
        ("atlassian_bangalore", "atlassian_bangalore_event.html", "https://ace.atlassian.com/events/details/atlassian-bangalore-presents-rovo/", "Atlassian Community"),
        ("google_search_central", "google_rsvp_event.html", "https://rsvp.withgoogle.com/events/search-central-live-bengaluru", "Google Search Central"),
        ("google_developers", "google_rsvp_event.html", "https://rsvp.withgoogle.com/events/google-developers", "Google Search Central"),
        ("databricks_events", "databricks_webinar_event.html", "https://www.databricks.com/resources/webinar/databricks-apac-learning-festival", "Databricks"),
    ],
)
def test_enabled_new_source_detail_fixtures_extract_factual_fields(
    source_name, fixture_name, page_url, expected_organizer
):
    source = next(source for source in get_sources_registry().sources if source.name == source_name)
    html = Path("tests/fixtures", fixture_name).read_text(encoding="utf-8")
    candidate = parse_event_page(html, page_url, source_name, platform=source.platform)[0]
    assert candidate.starts_at is not None
    assert candidate.organizer == expected_organizer
    assert candidate.registration_url and candidate.registration_url.startswith("https://")


@pytest.mark.asyncio
async def test_public_page_adapter_respects_robots_and_parses():
    fixture = Path("tests/fixtures/event_page.html").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text=fixture)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicPageEventSource(
            SourceDefinition(name="fixture", adapter="public_page", url="https://example.test/events", rate_limit_seconds=0),
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
            SourceDefinition(name="fixture", adapter="public_page", url="https://example.test/events", rate_limit_seconds=0),
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
        assert make_source(
            SourceDefinition(
                name="search",
                adapter="search",
                query="test",
                allowed_domains=["events.example.test"],
            ),
            client,
        ).__class__.__name__ == "SearchEventSource"
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
                rate_limit_seconds=0,
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
                rate_limit_seconds=0,
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
    # The cold-cache robots.txt fetch now shares the same per-origin cadence
    # (it is never a free first hit), so it consumes the origin's initial
    # zero-delay slot; page 1 and page 2 then each wait the full 3s behind it.
    assert delays == [3, 3]


@pytest.mark.asyncio
async def test_configured_detail_hydration_is_bounded_and_deduplicated():
    listing = Path("tests/fixtures/detail_listing.html").read_text(encoding="utf-8")
    detail = Path("tests/fixtures/detail_event.html").read_text(encoding="utf-8")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == "/listing":
            return httpx.Response(200, text=listing)
        if request.url.path in {"/events/a", "/events/b"}:
            return httpx.Response(200, text=detail)
        raise AssertionError(f"unexpected detail request: {request.url.path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicPageEventSource(
            SourceDefinition(
                name="details_fixture",
                adapter="public_page",
                url="https://events.example.test/listing",
                allowed_domains=["events.example.test"],
                detail_link_selectors=["a.event-detail"],
                detail_link_prefixes=["/events/"],
                max_detail_pages=2,
                rate_limit_seconds=0,
            ),
            client,
            RobotsPolicy(client, _safety()),
            _safety(),
        )
        result = await source.fetch()
    assert requested == ["/robots.txt", "/listing", "/events/a", "/events/b"]
    assert [candidate.canonical_url for candidate in result.candidates] == [
        "https://events.example.test/events/shared"
    ]
    assert [item.facts["page_kind"] for item in result.source_evidence] == [
        "listing",
        "detail",
        "detail",
    ]


@pytest.mark.asyncio
async def test_detail_hydration_merges_complementary_facts_conservatively():
    listing = """<script type='application/ld+json'>{
      "@type":"Event", "name":"Bengaluru AI Systems Meetup",
      "url":"/events/shared", "startDate":"2026-10-10T10:00:00+05:30",
      "location":{"name":"Bengaluru"}, "description":"Listing facts",
      "registrationDeadline":"2026-10-05T10:00:00+05:30", "price":"Free"
    }</script><a class='event-detail' href='/events/shared'>Details</a>"""
    detail = """<script type='application/ld+json'>{
      "@type":"Event", "name":"Bengaluru AI Systems Meetup",
      "url":"/events/shared", "endDate":"2026-10-10T18:00:00+05:30",
      "description":"Detail facts", "registrationStatus":"Closed",
      "offers":{"price":"499 INR"}
    }</script>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        if request.url.path == "/listing":
            return httpx.Response(200, text=listing)
        if request.url.path == "/events/shared":
            return httpx.Response(200, text=detail)
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicPageEventSource(
            SourceDefinition(
                name="merged_details",
                adapter="public_page",
                url="https://events.example.test/listing",
                allowed_domains=["events.example.test"],
                detail_link_selectors=["a.event-detail"],
                detail_link_prefixes=["/events/"],
                max_detail_pages=1,
                rate_limit_seconds=0,
            ),
            client,
            RobotsPolicy(client, _safety()),
            _safety(),
        )
        result = await source.fetch()
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.starts_at is not None
    assert candidate.ends_at is not None
    assert candidate.registration_deadline == datetime(2026, 10, 5, 4, 30, tzinfo=UTC)
    assert candidate.registration_state.value == "closed"
    assert candidate.price_text == "Free; 499 INR"
    assert candidate.is_explicitly_paid is True
    assert candidate.description == "Listing facts; Detail facts"
    assert candidate.evidence.facts["merged_source_urls"] == [
        "https://events.example.test/listing",
        "https://events.example.test/events/shared",
    ]
    observations = candidate.evidence.facts["merged_observations"]
    assert [observation["source_url"] for observation in observations] == [
        "https://events.example.test/listing",
        "https://events.example.test/events/shared",
    ]
    assert observations[0]["facts"]["event"]["description"] == "Listing facts"
    assert observations[0]["facts"]["event"]["registrationDeadline"] == "2026-10-05T10:00:00+05:30"
    assert observations[1]["facts"]["event"]["description"] == "Detail facts"
    assert all("merged_observations" not in observation["facts"] for observation in observations)


@pytest.mark.asyncio
@pytest.mark.parametrize("detail_outcome", ["robots", "redirect", "interstitial"])
async def test_detail_hydration_keeps_robots_redirect_and_interstitial_boundaries(
    detail_outcome,
):
    listing = '<a class="event-detail" href="/events/blocked">Blocked detail</a>'
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            robots = "User-agent: *\nDisallow: /events/blocked" if detail_outcome == "robots" else "User-agent: *\nAllow: /"
            return httpx.Response(200, text=robots)
        if request.url.path == "/listing":
            return httpx.Response(200, text=listing)
        if detail_outcome == "redirect":
            return httpx.Response(302, headers={"location": "https://outside.example.test/event"})
        if detail_outcome == "interstitial":
            return httpx.Response(200, text="Checking your browser before accessing")
        raise AssertionError("robots should reject the detail before requesting it")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicPageEventSource(
            SourceDefinition(
                name="details_boundary",
                adapter="public_page",
                url="https://events.example.test/listing",
                allowed_domains=["events.example.test"],
                detail_link_selectors=["a.event-detail"],
                detail_link_prefixes=["/events/"],
                max_detail_pages=1,
                rate_limit_seconds=0,
            ),
            client,
            RobotsPolicy(client, _safety()),
            _safety(),
        )
        result = await source.fetch()
    assert not result.candidates
    assert result.source_evidence[-1].facts["page_kind"] == "detail"
    assert "rejected" in result.source_evidence[-1].facts
    if detail_outcome == "robots":
        assert requested == ["/robots.txt", "/listing"]
    else:
        assert requested == ["/robots.txt", "/listing", "/events/blocked"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("registration_url", "registration_domains", "expected_registration_url"),
    [
        ("https://tickets.example.test/event", [], None),
        (
            "https://rsvp.withgoogle.com/events/search-central",
            ["rsvp.withgoogle.com"],
            "https://rsvp.withgoogle.com/events/search-central",
        ),
        (
            "https://register.opensourceindia.in/2026",
            ["opensourceindia.in"],
            "https://register.opensourceindia.in/2026",
        ),
    ],
)
async def test_registration_urls_need_a_separate_allowlist(
    registration_url, registration_domains, expected_registration_url
):
    html = f"""<script type='application/ld+json'>{{
      "@type":"Event", "name":"Bengaluru AI Meetup",
      "url":"https://events.example.test/event",
      "startDate":"2026-10-10T10:00:00+05:30",
      "location":{{"name":"Bengaluru"}},
      "registrationUrl":"{registration_url}"
    }}</script>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        raise AssertionError(f"registration URLs are not fetched: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicPageEventSource(
            SourceDefinition(
                name="registration_fixture",
                adapter="public_page",
                url="https://events.example.test/listing",
                allowed_domains=["events.example.test"],
                allowed_registration_domains=registration_domains,
            ),
            client,
            RobotsPolicy(client, _safety()),
            _safety(),
        )
        candidates = await source.validated_candidates(
            parse_event_page(html, "https://events.example.test/listing", "registration_fixture")
        )
    assert len(candidates) == 1
    assert candidates[0].registration_url == expected_registration_url


@pytest.mark.asyncio
async def test_disallowed_registration_host_is_rejected_before_dns_resolution():
    resolved_hosts: list[str] = []

    async def resolver(hostname: str) -> list[str]:
        resolved_hosts.append(hostname)
        return ["93.184.216.34"]

    html = """<script type='application/ld+json'>{
      "@type":"Event", "name":"Bengaluru AI Meetup",
      "url":"https://events.example.test/event",
      "startDate":"2026-10-10T10:00:00+05:30",
      "location":{"name":"Bengaluru"},
      "registrationUrl":"https://tickets.example.test/event"
    }</script>"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        raise AssertionError(f"registration URL was fetched: {request.url}")

    safety = URLSafety(resolver)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicPageEventSource(
            SourceDefinition(
                name="registration_dns_boundary",
                adapter="public_page",
                url="https://events.example.test/listing",
                allowed_domains=["events.example.test"],
            ),
            client,
            RobotsPolicy(client, safety),
            safety,
        )
        candidates = await source.validated_candidates(
            parse_event_page(html, "https://events.example.test/listing", "registration_dns_boundary")
        )
    assert candidates[0].registration_url is None
    assert "tickets.example.test" not in resolved_hosts


def test_redirect_transport_domain_is_not_an_implicit_registration_allowlist():
    definition = next(
        source for source in get_sources_registry().sources if source.name == "cncf_bengaluru"
    )
    source = PublicPageEventSource(
        definition,
        httpx.AsyncClient(),
        safety=_safety(),
    )
    try:
        assert "ocgroups.dev" in source.allowed_domains
        assert "ocgroups.dev" not in source.allowed_registration_domains
        assert source.allowed_registration_domains == {"community.cncf.io"}
    finally:
        asyncio.run(source.client.aclose())


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


@pytest.mark.asyncio
async def test_response_over_max_bytes_is_rejected_before_it_reaches_parsing():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="x" * 5000)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceFetchError, match="response exceeded max size"):
            await _request_redirect_checked(
                client, "https://events.example.test/big", _safety(), max_bytes=1000
            )


@pytest.mark.asyncio
async def test_response_under_max_bytes_is_read_and_decoded_normally():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="a small page body")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        response = await _request_redirect_checked(
            client, "https://events.example.test/small", _safety(), max_bytes=1000
        )
    assert response.text == "a small page body"
    assert response.status_code == 200
    assert str(response.url) == "https://events.example.test/small"


@pytest.mark.asyncio
async def test_default_max_response_bytes_cap_rejects_an_oversized_listing_page():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /")
        return httpx.Response(200, text="y" * (MAX_RESPONSE_BYTES + 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = PublicPageEventSource(
            SourceDefinition(name="fixture", adapter="public_page", url="https://example.test/events", rate_limit_seconds=0),
            client,
            RobotsPolicy(client, _safety()),
            _safety(),
        )
        with pytest.raises(SourceFetchError, match="response exceeded max size"):
            await source.fetch()


@pytest.mark.asyncio
async def test_robots_failure_is_negatively_cached_and_not_immediately_refetched():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(503)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        policy = RobotsPolicy(client, _safety())
        assert not await policy.allows("https://events.example.test/a")
        assert not await policy.allows("https://events.example.test/b")
    # The failed robots.txt fetch is cached (fail-closed) so a second path
    # check within the negative TTL never re-hammers the struggling origin.
    assert calls == ["/robots.txt"]


@pytest.mark.asyncio
async def test_robots_fetch_times_out_and_still_fails_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("robots.txt timed out")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        policy = RobotsPolicy(client, _safety())
        assert not await policy.allows("https://events.example.test/a")


@pytest.mark.asyncio
async def test_robots_fetch_is_routed_through_the_source_rate_limiter():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="User-agent: *\nAllow: /")

    waits: list[tuple[str, float]] = []

    class _RecordingLimiter(RequestLimiter):
        async def wait(self, url: str, seconds: float) -> None:
            waits.append((url, seconds))
            await super().wait(url, seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        policy = RobotsPolicy(client, _safety())
        limiter = _RecordingLimiter(sleeper=lambda _delay: asyncio.sleep(0))
        await policy.allows("https://events.example.test/a", limiter=limiter, rate_limit_seconds=2)
    assert waits == [("https://events.example.test/robots.txt", 2)]


@pytest.mark.asyncio
async def test_robots_crawl_delay_increases_the_limiter_wait_beyond_configured_cadence():
    fixture = Path("tests/fixtures/platform_event.html").read_text(encoding="utf-8")
    elapsed = [0.0]
    delays: list[float] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)
        elapsed[0] += delay

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\nCrawl-delay: 5")
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
                rate_limit_seconds=1,
            ),
            client,
            RobotsPolicy(client, _safety()),
            _safety(),
            RequestLimiter(sleeper=sleep, clock=lambda: elapsed[0]),
        )
        await source.fetch()
    # The configured cadence (1s) is below the origin's declared Crawl-delay
    # (5s); once robots.txt has been loaded, every subsequent same-origin
    # wait must honor the larger crawl-delay floor, not the smaller cadence.
    assert delays == [1, 5]


class _FakeSearchSettings:
    def __init__(
        self,
        *,
        serper_api_key: str | None = None,
        brave_search_api_key: str | None = None,
        tavily_api_key: str | None = None,
        exa_api_key: str | None = None,
    ):
        self.serper_api_key = serper_api_key
        self.brave_search_api_key = brave_search_api_key
        self.tavily_api_key = tavily_api_key
        self.exa_api_key = exa_api_key


def _search_source(client: httpx.AsyncClient) -> SearchEventSource:
    return SearchEventSource(
        SourceDefinition(
            name="search",
            adapter="search",
            query="technical events",
            allowed_domains=["events.example.test"],
        ),
        client,
    )


@pytest.mark.asyncio
async def test_ddgs_failure_falls_back_to_serper_success_shape(monkeypatch):
    monkeypatch.setattr(
        "eventfinder.sources.get_settings",
        lambda: _FakeSearchSettings(serper_api_key="key"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "google.serper.dev"
        assert request.headers["x-api-key"] == "key"
        return httpx.Response(200, json={"organic": [{"link": "https://events.example.test/a"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await _search_source(client)._fallback_results("query", RuntimeError("ddgs down"))
    assert results == [{"href": "https://events.example.test/a"}]


@pytest.mark.asyncio
async def test_ddgs_failure_falls_back_to_brave_success_shape(monkeypatch):
    monkeypatch.setattr(
        "eventfinder.sources.get_settings",
        lambda: _FakeSearchSettings(brave_search_api_key="key"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.search.brave.com"
        return httpx.Response(200, json={"web": {"results": [{"url": "https://events.example.test/b"}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await _search_source(client)._fallback_results("query", RuntimeError("ddgs down"))
    assert results == [{"href": "https://events.example.test/b"}]


@pytest.mark.asyncio
async def test_ddgs_failure_falls_back_to_tavily_success_shape(monkeypatch):
    monkeypatch.setattr(
        "eventfinder.sources.get_settings",
        lambda: _FakeSearchSettings(tavily_api_key="key"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.tavily.com"
        return httpx.Response(200, json={"results": [{"url": "https://events.example.test/c"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await _search_source(client)._fallback_results("query", RuntimeError("ddgs down"))
    assert results == [{"href": "https://events.example.test/c"}]


@pytest.mark.asyncio
async def test_ddgs_failure_falls_back_to_exa_success_shape(monkeypatch):
    monkeypatch.setattr(
        "eventfinder.sources.get_settings",
        lambda: _FakeSearchSettings(exa_api_key="key"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.exa.ai"
        return httpx.Response(200, json={"results": [{"url": "https://events.example.test/d"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await _search_source(client)._fallback_results("query", RuntimeError("ddgs down"))
    assert results == [{"href": "https://events.example.test/d"}]


@pytest.mark.asyncio
async def test_serper_4xx_with_no_further_providers_raises_source_fetch_error(monkeypatch):
    monkeypatch.setattr(
        "eventfinder.sources.get_settings",
        lambda: _FakeSearchSettings(serper_api_key="key"),
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(400))
    ) as client:
        with pytest.raises(SourceFetchError, match="search unavailable"):
            await _search_source(client)._fallback_results("query", RuntimeError("ddgs down"))


@pytest.mark.asyncio
async def test_brave_timeout_with_no_further_providers_raises_source_fetch_error(monkeypatch):
    monkeypatch.setattr(
        "eventfinder.sources.get_settings",
        lambda: _FakeSearchSettings(brave_search_api_key="key"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("brave timed out")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(SourceFetchError, match="search unavailable"):
            await _search_source(client)._fallback_results("query", RuntimeError("ddgs down"))


@pytest.mark.asyncio
async def test_all_search_fallback_providers_failing_raises_source_fetch_error(monkeypatch):
    monkeypatch.setattr(
        "eventfinder.sources.get_settings",
        lambda: _FakeSearchSettings(
            serper_api_key="s", brave_search_api_key="b", tavily_api_key="t", exa_api_key="e"
        ),
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    ) as client:
        with pytest.raises(SourceFetchError, match="search unavailable"):
            await _search_source(client)._fallback_results("query", RuntimeError("ddgs down"))


@pytest.mark.asyncio
async def test_search_fetch_hydrates_serper_fallback_results_end_to_end(monkeypatch):
    fixture = Path("tests/fixtures/platform_event.html").read_text(encoding="utf-8")

    class _RaisingSearch:
        def text(self, *_args, **_kwargs):
            raise RuntimeError("ddgs unavailable")

    monkeypatch.setattr("eventfinder.sources.DDGS", _RaisingSearch)
    monkeypatch.setattr(
        "eventfinder.sources.get_settings",
        lambda: _FakeSearchSettings(serper_api_key="key"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "google.serper.dev":
            return httpx.Response(
                200, json={"organic": [{"link": "https://events.example.test/platform-fixture"}]}
            )
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
                rate_limit_seconds=0,
            ),
            client,
            RobotsPolicy(client, _safety()),
            _safety(),
        )
        result = await source.fetch()
    assert result.candidates[0].canonical_url == "https://events.example.test/platform-fixture"
