from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup
from eventfinder.config import SourceDefinition, get_sources_registry
from eventfinder.domain import candidate_admission_status
from eventfinder.sources import (
    PublicPageEventSource,
    RobotsPolicy,
    SourceFetchError,
    _is_interstitial,
    parse_event_page,
)
from eventfinder.urls import URLSafety

FIXTURES = Path("tests/fixtures")
NOW = datetime(2026, 10, 8, 6, tzinfo=UTC)
CLOSED_MODAL = (
    '<div class="modal fade" id="contact-host-modal" role="dialog">'
    '<form class="contact-host"><span class="g-recaptcha"></span></form></div>'
)


async def _public_resolver(_hostname: str) -> list[str]:
    return ["93.184.216.34"]


def _safety() -> URLSafety:
    return URLSafety(_public_resolver)


def _definition(name: str) -> SourceDefinition:
    source = next(source for source in get_sources_registry().sources if source.name == name)
    return source.model_copy(update={"rate_limit_seconds": 0})


def _source(definition: SourceDefinition, handler) -> tuple[PublicPageEventSource, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return PublicPageEventSource(definition, client, RobotsPolicy(client, _safety()), _safety()), client


@pytest.mark.parametrize(
    "html",
    [
        f"<html><body><h1>Events</h1>{CLOSED_MODAL}</body></html>",
    ],
)
def test_closed_contact_form_captcha_is_not_a_page_challenge(html):
    assert _is_interstitial(html) is False


@pytest.mark.parametrize(
    "html",
    [
        "<div class='modal fade show'><form><div class='g-recaptcha'></div></form></div>",
        "<div class='modal' aria-modal='true'><form><div class='g-recaptcha'></div></form></div>",
        "<form><div class='g-recaptcha'></div></form>",
        "<div class='modal fade'><div class='g-recaptcha'></div></div>",
        "<dialog open><form><div class='g-recaptcha'></div></form></dialog>",
        "<dialog><form><div class='g-recaptcha'></div></form></dialog>",
        "<div class='modal fade' style='display:block'><form><div class='g-recaptcha'></div></form></div>",
        f"<body>{CLOSED_MODAL}<div class='h-captcha'></div></body>",
    ],
)
def test_visible_or_unscoped_captcha_widgets_remain_interstitials(html):
    assert _is_interstitial(html) is True


def test_luma_item_list_yields_json_ld_events_without_group_metadata():
    html = (FIXTURES / "luma_bengaluru_listing.html").read_text(encoding="utf-8")
    candidates = parse_event_page(html, "https://luma.com/bengaluru", "luma_bengaluru", NOW, "luma")
    assert len(candidates) == 2
    assert {candidate.evidence.facts["parser"] for candidate in candidates} == {"json_ld"}
    assert all(candidate_admission_status(candidate) == "free" for candidate in candidates)
    assert sorted(candidate.registration_state.value for candidate in candidates) == ["open", "sold_out"]
    assert {candidate.city for candidate in candidates} == {"Bengaluru"}


def test_item_list_without_event_items_yields_no_candidates():
    html = (
        '<script type="application/ld+json">{"@type": "BreadcrumbList", "itemListElement": ['
        '{"@type": "ListItem", "position": 1, "name": "Home", "item": "https://luma.com/"},'
        '{"@type": "ListItem", "position": 2, "name": "Bengaluru", "item": {"@id": "https://luma.com/bengaluru"}}'
        "]}</script>"
    )
    assert parse_event_page(html, "https://luma.com/bengaluru", "luma_bengaluru", NOW, "luma") == []


@pytest.mark.asyncio
async def test_luma_old_domain_fails_closed_on_cross_domain_robots_redirect():
    old = SourceDefinition(
        name="luma_old", adapter="public_page", platform="luma", url="https://lu.ma/bangalore",
        allowed_domains=["lu.ma"], rate_limit_seconds=0,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "lu.ma" and request.url.path == "/robots.txt":
            return httpx.Response(301, headers={"location": "https://luma.com/robots.txt"})
        return httpx.Response(200, text="User-agent: Googlebot\nDisallow: /in/\n")

    source, client = _source(old, handler)
    async with client:
        with pytest.raises(SourceFetchError, match="robots policy disallows this URL"):
            await source.fetch()


@pytest.mark.asyncio
async def test_luma_canonical_domain_fetches_listing_and_bounded_details():
    listing = (FIXTURES / "luma_bengaluru_listing.html").read_text(encoding="utf-8")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: Googlebot\nDisallow: /in/\n")
        if request.url.path == "/bengaluru":
            return httpx.Response(200, text=listing)
        return httpx.Response(200, text="<html><body><h1>Event</h1></body></html>")

    definition = _definition("luma_bengaluru")
    source, client = _source(definition, handler)
    async with client:
        result = await source.fetch()
    assert requested == ["/robots.txt", "/bengaluru", "/abc12345", "/def67890"]
    assert len(requested) - 2 <= definition.max_detail_pages
    assert len(result.candidates) == 2


@pytest.mark.asyncio
async def test_bevy_chapter_hydrates_only_its_own_upcoming_events():
    listing = (FIXTURES / "bevy_chapter_listing.html").read_text(encoding="utf-8")
    event = (FIXTURES / "bevy_chapter_event.html").read_text(encoding="utf-8")
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        if request.url.path == "/bengaluru/":
            return httpx.Response(200, text=listing)
        if request.url.path == "/events/details/uipath-bengaluru-presents-agentic-day/":
            return httpx.Response(200, text=event)
        raise AssertionError(f"unexpected request: {request.url.path}")

    source, client = _source(_definition("uipath_bengaluru"), handler)
    async with client:
        result = await source.fetch()
    assert requested == [
        "/robots.txt", "/bengaluru/", "/events/details/uipath-bengaluru-presents-agentic-day/",
    ]
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.evidence.facts["parser"] == "json_ld"
    assert candidate_admission_status(candidate) == "free"


def _count_upcoming(html: str, definition: SourceDefinition) -> tuple[int, int]:
    soup = BeautifulSoup(html, "html.parser")
    upcoming = {a["href"] for a in soup.select(definition.detail_link_selectors[0])}
    everything = {a["href"] for a in soup.select(f"a[href*='{definition.detail_link_prefixes[0]}']")}
    return len(upcoming), len(everything - upcoming)


@pytest.mark.parametrize("source_name", ["atlassian_bangalore", "gdg_bengaluru"])
def test_existing_chapter_selectors_match_only_upcoming_section(source_name):
    definition = _definition(source_name)
    prefix = definition.detail_link_prefixes[0]
    html = f"""
    <div><div><h1 role="heading">Upcoming events</h1></div><div>
      <a href="https://example.test{prefix}new-one/">New</a>
      <a href="https://example.test/events/details/other-chapter-presents-x/">Other</a>
    </div></div>
    <div><div><h1 role="heading">Past events</h1></div><div>
      <a href="https://example.test{prefix}old-one/">Old</a>
    </div></div>"""
    assert _count_upcoming(html, definition) == (1, 1)
    soup = BeautifulSoup(html, "html.parser")
    assert [a["href"] for a in soup.select(definition.detail_link_selectors[0])] == [
        f"https://example.test{prefix}new-one/"
    ]


def test_atlassian_selector_ignores_a_past_only_chapter_page():
    html = """<div><div><h1 role="heading">Past events</h1></div><div>
      <a href="https://ace.atlassian.com/events/details/atlassian-bangalore-presents-old/">Old</a>
    </div></div>"""
    assert _count_upcoming(html, _definition("atlassian_bangalore")) == (0, 1)


def _og_only_listing() -> str:
    return '<html><head><meta property="og:title" content="Chapter calendar"></head><body></body></html>'


@pytest.mark.asyncio
async def test_listing_opengraph_is_suppressed_only_when_details_are_hydrated():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        return httpx.Response(200, text=_og_only_listing())

    base = {
        "name": "og_fixture", "adapter": "public_page", "url": "https://events.example.test/listing",
        "allowed_domains": ["events.example.test"], "rate_limit_seconds": 0,
    }
    hydrating = SourceDefinition(**base, detail_link_selectors=["a.detail"], max_detail_pages=2)
    source, client = _source(hydrating, handler)
    async with client:
        assert (await source.fetch()).candidates == []
    plain = SourceDefinition(**base)
    source, client = _source(plain, handler)
    async with client:
        result = await source.fetch()
    assert [c.evidence.facts["parser"] for c in result.candidates] == ["opengraph"]


def test_gdg_bengaluru_selector_matches_calendar_fixture_upcoming_link_only():
    html = (FIXTURES / "gdg_bangalore_calendar.html").read_text(encoding="utf-8")
    assert _count_upcoming(html, _definition("gdg_bengaluru")) == (1, 1)


def test_inline_display_none_modal_stays_dormant():
    html = "<div class='modal' style='display: none'><form><div class='g-recaptcha'></div></form></div>"
    assert _is_interstitial(html) is False
