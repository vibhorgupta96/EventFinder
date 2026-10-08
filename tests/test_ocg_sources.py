from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from bs4 import BeautifulSoup
from eventfinder.config import SourceDefinition, SourcesRegistry, get_sources_registry
from eventfinder.domain import (
    EventFormat,
    EventType,
    RegistrationState,
    admission_price_status,
    candidate_admission_status,
)
from eventfinder.models import Event, EventSource
from eventfinder.service import DiscoveryService, _observed_event_provenance
from eventfinder.sources import (
    PublicPageEventSource,
    RobotsPolicy,
    SourceFetchError,
    _configured_detail_urls,
    _ocg_end_time,
    _ocg_event_candidates,
    parse_event_page,
)
from eventfinder.urls import URLSafety
from sqlmodel import Session, select

FIXTURES = Path("tests/fixtures")
GROUP_URL = "https://ocgroups.dev/cncf/group/52r68y4"
UPCOMING_URL = "https://ocgroups.dev/cncf/group/96ne96f/event/pre83n8"
PAST_URL = "https://ocgroups.dev/cncf/group/52r68y4/event/quau4tx"
NOW = datetime(2026, 10, 8, 6, tzinfo=UTC)
STARTS = 'data-starts="2026-07-26T02:30:00+00:00"'


async def _resolver(_host: str) -> list[str]:
    return ["93.184.216.34"]


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _swap(html: str, old: str, new: str, count: int = 1) -> str:
    assert html.count(old) == count, old
    return html.replace(old, new)


def _definition() -> SourceDefinition:
    return next(s for s in get_sources_registry().sources if s.name == "cncf_bengaluru")


def _parse(html: str, url: str = PAST_URL):
    return parse_event_page(html, url, "cncf_bengaluru", NOW, "ocg")


def _one(html: str, url: str = PAST_URL):
    (candidate,) = _parse(html, url)
    return candidate


def test_cncf_bengaluru_uses_canonical_ocg_page_with_upcoming_only_selectors():
    definition = _definition()
    assert definition.platform == "ocg"
    assert definition.url == GROUP_URL
    assert definition.allowed_domains == ["ocgroups.dev"]
    assert definition.detail_link_selectors == [
        "div:has(> div:-soup-contains-own('Next event')) > a[href*='/cncf/group/52r68y4/event/']",
        "div:has(> div > div:-soup-contains-own('Upcoming Events')) a[href*='/cncf/group/52r68y4/event/']",
    ]
    assert definition.detail_link_prefixes == ["/cncf/group/52r68y4/event/"]
    assert definition.max_detail_pages == 4
    assert definition.priority is True
    client = httpx.AsyncClient()
    try:
        source = PublicPageEventSource(definition, client, safety=URLSafety(_resolver))
        assert source.allowed_registration_domains == {"ocgroups.dev"}
    finally:
        asyncio.run(client.aclose())


def test_ocg_selectors_hydrate_only_the_next_upcoming_event():
    html = _read("ocg_group_upcoming.html")
    assert html.count("/event/") >= 7
    html = html.replace("96ne96f", "52r68y4")
    assert _configured_detail_urls(html, GROUP_URL, _definition()) == [
        "https://ocgroups.dev/cncf/group/52r68y4/event/pre83n8"
    ]


def test_ocg_group_without_upcoming_events_hydrates_nothing():
    html = _read("ocg_group_no_upcoming.html")
    assert html.count("/cncf/group/52r68y4/event/") >= 6
    assert _configured_detail_urls(html, GROUP_URL, _definition()) == []


def test_ocg_upcoming_event_reads_attendance_attributes():
    c = _one(_read("ocg_event_upcoming.html"), UPCOMING_URL)
    assert c.title == "Cloud Native Chennai - October 2026 Meetup"
    assert c.organizer == "Cloud Native Chennai"
    assert c.starts_at == datetime(2026, 10, 10, 8, 0, tzinfo=UTC)
    assert c.ends_at == datetime(2026, 10, 10, 12, 15, tzinfo=UTC)
    assert c.venue == "Workday India Private Ltd, Chennai, Tamil Nadu, India"
    assert c.city is None
    assert c.format == EventFormat.IN_PERSON
    assert c.event_type == EventType.MEETUP
    assert c.registration_state == RegistrationState.OPEN
    assert c.registration_url == UPCOMING_URL
    assert c.canonical_url == UPCOMING_URL
    assert c.price_text == "Free"
    assert c.is_explicitly_paid is False
    assert admission_price_status(c.price_text) == "free"
    # The approval note "registration 3 days before the event" is timing, not a fee.
    assert "registration 3 days before the event" in c.description
    assert candidate_admission_status(c) == "free"
    assert c.eligibility_text == "Attendee approval required"
    assert c.evidence.raw_id == "8647b88c-9426-49e0-b5ee-fff8ee4f7e98"
    assert len(c.description) == 1236
    facts = c.evidence.facts
    assert facts["parser"] == "ocg:attendance"
    assert facts["ocg_attendance"]["registration_window_open"] == "true"
    assert facts["ocg_attendance"]["attendee_approval_required"] == "true"
    assert facts["ocg_attendance"]["event_timezone"] == "Asia/Kolkata"
    assert facts["ocg_tickets"] == [{"price_minor": "0", "sold_out": "false", "purchasable": "true"}]


def test_ocg_past_event_is_closed_not_cancelled():
    c = _one(_read("ocg_event_past.html"))
    assert c.evidence.facts["parser"] == "ocg:attendance"
    assert c.registration_state != RegistrationState.CANCELLED
    assert c.starts_at is not None
    assert c.title == "Kubernetes Bangalore meetup"
    assert c.organizer == "Cloud Native Bangalore"
    assert c.starts_at == datetime(2026, 7, 26, 2, 30, tzinfo=UTC)
    assert c.ends_at == datetime(2026, 7, 26, 8, 30, tzinfo=UTC)
    assert c.venue == "VMware by Broadcom, Bengaluru"
    assert c.city == "Bengaluru"
    assert c.format == EventFormat.IN_PERSON
    assert c.event_type == EventType.WORKSHOP
    assert c.registration_state == RegistrationState.CLOSED
    assert c.registration_url == PAST_URL
    assert c.canonical_url == PAST_URL
    assert c.price_text == "Free"
    assert c.is_explicitly_paid is False
    assert c.eligibility_text is None
    assert c.evidence.raw_id == "64fb75a6-ee55-4a3f-adc1-23472c39a768"
    assert len(c.description) == 1036
    attendance = c.evidence.facts["ocg_attendance"]
    assert attendance["registration_window_open"] == "false"
    assert attendance["event_timezone"] == "Asia/Calcutta"
    assert attendance["registration_window_message"] == "Registration closed Jul 25, 2026 at 11:30 PM IST."
    assert c.evidence.facts["ocg_tickets"] == [{"price_minor": "0", "sold_out": "false", "purchasable": "true"}]
    assert admission_price_status(c.price_text) == "free"
    assert candidate_admission_status(c) == "free"


@pytest.mark.parametrize(
    ("edits", "expected"),
    [
        ([('data-canceled="false"', 'data-canceled="true"')], RegistrationState.CANCELLED),
        ([('data-ticket-sold-out="false"', 'data-ticket-sold-out="true"')], RegistrationState.SOLD_OUT),
        (
            [
                ('data-ticket-sold-out="false"', 'data-ticket-sold-out="true"'),
                ('data-waitlist-enabled="false"', 'data-waitlist-enabled="true"'),
            ],
            RegistrationState.WAITLIST,
        ),
        ([('data-registration-window-open="false"', "")], RegistrationState.UNKNOWN),
        (
            [
                ('data-canceled="false"', 'data-canceled="true"'),
                ('data-ticket-sold-out="false"', 'data-ticket-sold-out="true"'),
            ],
            RegistrationState.CANCELLED,
        ),
        (
            [(
                'data-registration-window-message="Registration closed Jul 25, 2026 at 11:30 PM IST."',
                'data-registration-window-message="Registration opens Oct 1, 2026 at 9:00 AM IST."',
            )],
            RegistrationState.UNKNOWN,
        ),
        (
            [(
                'data-registration-window-message="Registration closed Jul 25, 2026 at 11:30 PM IST."',
                'data-registration-window-message=""',
            )],
            RegistrationState.UNKNOWN,
        ),
        (
            [
                ('data-ticket-sold-out="false"', 'data-ticket-sold-out="true"'),
                ('data-registration-window-open="false"', 'data-registration-window-open="true"'),
            ],
            RegistrationState.SOLD_OUT,
        ),
    ],
    ids=["canceled", "sold_out", "waitlist", "window_attr_missing", "canceled_and_sold_out",
         "opens_message", "empty_message", "sold_out_window_open"],
)
def test_ocg_registration_state_from_attendance_flags(edits, expected):
    html = _read("ocg_event_past.html")
    for old, new in edits:
        html = _swap(html, old, new)
    assert _one(html).registration_state == expected


def test_ocg_positive_ticket_price_is_paid():
    html = _swap(_read("ocg_event_past.html"), 'data-ticket-price-minor="0"', 'data-ticket-price-minor="50000"')
    html = _swap(html, 'data-ticket-is-free-only="true"', 'data-ticket-is-free-only="false"')
    c = _one(html)
    assert c.price_text is None
    assert c.is_explicitly_paid is True
    assert candidate_admission_status(c) == "paid"


def test_ocg_free_only_flag_with_nonzero_ticket_is_not_free():
    html = _swap(_read("ocg_event_past.html"), 'data-ticket-price-minor="0"', 'data-ticket-price-minor="50000"')
    c = _one(html)
    assert c.is_explicitly_paid is True
    assert c.price_text != "Free"


def test_ocg_paid_capable_event_is_not_stated():
    html = _swap(_read("ocg_event_past.html"), 'data-paid-capable="false"', 'data-paid-capable="true"')
    c = _one(html)
    assert c.price_text is None
    assert admission_price_status(c.price_text) == "not_stated"


@pytest.mark.parametrize("replacement", ["", 'data-starts="not-a-date"'], ids=["missing", "invalid"])
def test_ocg_owned_widget_without_start_yields_no_candidates(replacement):
    html = _swap(_read("ocg_event_past.html"), STARTS, replacement)
    assert _parse(html) == []


@pytest.mark.parametrize(
    "edit",
    [
        ('id="attendance-container-main"', 'id="renamed-container"'),
        ("data-attendance-container=", "data-renamed-container="),
    ],
    ids=["renamed_id", "dropped_attribute"],
)
def test_ocg_event_page_with_drifted_container_fails_closed(edit):
    html = _swap(_read("ocg_event_past.html"), *edit)
    soup = BeautifulSoup(html, "html.parser")
    assert _ocg_event_candidates(soup, PAST_URL, "cncf_bengaluru", NOW) == []
    assert _parse(html) == []


def test_ocg_non_event_page_without_container_uses_existing_parsers():
    html = _read("ocg_group_no_upcoming.html")
    soup = BeautifulSoup(html, "html.parser")
    assert _ocg_event_candidates(soup, GROUP_URL, "cncf_bengaluru", NOW) is None
    candidates = _parse(html, GROUP_URL)
    assert candidates != []
    assert all(c.evidence.facts.get("parser") != "ocg:attendance" for c in candidates)


@pytest.mark.parametrize("where", ["before_header", "after_header"])
def test_ocg_header_scope_ignores_other_headings_and_badges(where):
    decoy = '<div><h1>Takeaways:</h1><span class="custom-badge">virtual</span></div>'
    html = _read("ocg_event_past.html")
    if where == "before_header":
        grid = '<div class="grid gap-x-6 gap-y-4 md:gap-y-0 md:grid-cols-[auto_minmax(0,1fr)_auto]"'
        html = _swap(html, grid, decoy + grid)
    else:
        html = _swap(html, "</main>", decoy + "</main>")
    c = _one(html)
    assert c.title == "Kubernetes Bangalore meetup"
    assert c.format == EventFormat.IN_PERSON


def test_ocg_header_badge_sets_online_format():
    html = _swap(_read("ocg_event_past.html"), ">in-person<", ">virtual<")
    assert _one(html).format == EventFormat.ONLINE


def test_ocg_organizer_requires_matching_group_path():
    c = _one(_read("ocg_event_past.html"), "https://ocgroups.dev/cncf/group/zzzz999/event/quau4tx")
    assert c.organizer is None


def test_ocg_end_time_requires_matching_displayed_start():
    html = _swap(_read("ocg_event_past.html"), "08:00 AM - 02:00 PM IST", "09:00 AM - 02:00 PM IST")
    c = _one(html)
    assert c.ends_at is None
    assert c.starts_at == datetime(2026, 7, 26, 2, 30, tzinfo=UTC)
    soup = BeautifulSoup(_read("ocg_event_past.html"), "html.parser")
    panel = soup.select_one("[data-registration-window-date-panel]")
    starts = datetime(2026, 7, 26, 2, 30, tzinfo=UTC)
    assert _ocg_end_time(panel, starts, "Asia/Calcutta") == datetime(2026, 7, 26, 8, 30, tzinfo=UTC)
    assert _ocg_end_time(panel, starts + timedelta(hours=1), "Asia/Calcutta") is None
    assert _ocg_end_time(panel, starts, "Not/AZone") is None
    assert _ocg_end_time(None, starts, "Asia/Calcutta") is None


def test_ocg_location_ignores_map_dialog():
    soup = BeautifulSoup(_read("ocg_event_past.html"), "html.parser")
    emptied = 0
    for pill in soup.select("div.absolute.bottom-2.left-2"):
        if pill.find_parent(attrs={"role": "dialog"}) is None:
            pill.clear()
            emptied += 1
    assert emptied == 2
    assert any(
        pill.find_parent(attrs={"role": "dialog"}) is not None and pill.get_text(strip=True)
        for pill in soup.select("div.absolute.bottom-2.left-2")
    )
    c = _one(str(soup))
    assert c.venue is None
    assert c.city is None


@pytest.mark.asyncio
async def test_old_community_url_fails_closed_on_cross_domain_robots_redirect():
    definition = SourceDefinition(
        name="cncf_old", adapter="public_page", platform="official",
        url="https://community.cncf.io/cloud-native-bangalore/",
        allowed_domains=["community.cncf.io", "ocgroups.dev"], rate_limit_seconds=0,
    )
    requested = []

    def handler(request):
        requested.append(str(request.url))
        if request.url.host == "community.cncf.io":
            return httpx.Response(301, headers={"location": "https://cncf.redirects.ocgroups.dev/robots.txt"})
        if request.url.host == "cncf.redirects.ocgroups.dev":
            return httpx.Response(308, headers={"location": "https://community2.cncf.io/robots.txt"})
        return httpx.Response(200, text="User-agent: *\nDisallow: /health/\n")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    safety = URLSafety(_resolver)
    source = PublicPageEventSource(definition, client, RobotsPolicy(client, safety), safety)
    try:
        with pytest.raises(SourceFetchError, match="robots policy disallows"):
            await source.fetch()
    finally:
        await client.aclose()
    assert requested == [
        "https://community.cncf.io/robots.txt",
        "https://cncf.redirects.ocgroups.dev/robots.txt",
    ]


async def _fetch(pages: dict[str, str]):
    requested = []

    def handler(request):
        requested.append((request.url.host, request.url.path))
        if request.url.path == "/robots.txt":
            return httpx.Response(404, text="Page not found")
        if request.url.path in pages:
            return httpx.Response(200, text=pages[request.url.path])
        raise AssertionError(str(request.url))

    definition = _definition().model_copy(update={"rate_limit_seconds": 0})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    safety = URLSafety(_resolver)
    source = PublicPageEventSource(definition, client, RobotsPolicy(client, safety), safety)
    try:
        result = await source.fetch()
    finally:
        await client.aclose()
    return requested, result


@pytest.mark.asyncio
async def test_cncf_bengaluru_fetches_listing_and_own_upcoming_detail():
    listing = _read("ocg_group_upcoming.html").replace("96ne96f", "52r68y4")
    event = _read("ocg_event_upcoming.html").replace("96ne96f", "52r68y4")
    requested, result = await _fetch({
        "/cncf/group/52r68y4": listing,
        "/cncf/group/52r68y4/event/pre83n8": event,
    })
    assert requested == [
        ("ocgroups.dev", "/robots.txt"),
        ("ocgroups.dev", "/cncf/group/52r68y4"),
        ("ocgroups.dev", "/cncf/group/52r68y4/event/pre83n8"),
    ]
    (c,) = result.candidates
    assert c.evidence.facts["parser"] == "ocg:attendance"
    assert c.registration_state == RegistrationState.OPEN
    assert c.registration_url == "https://ocgroups.dev/cncf/group/52r68y4/event/pre83n8"
    assert c.organizer == "Cloud Native Chennai"


@pytest.mark.asyncio
async def test_cncf_bengaluru_without_upcoming_events_returns_no_candidates():
    requested, result = await _fetch({GROUP_URL.removeprefix("https://ocgroups.dev"): _read("ocg_group_no_upcoming.html")})
    assert requested == [("ocgroups.dev", "/robots.txt"), ("ocgroups.dev", "/cncf/group/52r68y4")]
    assert result.candidates == []


def test_ocg_attendance_is_trusted_observed_event_provenance():
    source = EventSource(event_id=1, source_name="cncf_bengaluru", source_url=PAST_URL,
                         evidence={"parser": "ocg:attendance"})
    assert _observed_event_provenance(source, _definition(), PAST_URL) is True


@pytest.mark.asyncio
async def test_ocg_refresh_marks_cancelled_event(session, config, organizers):
    starts = (datetime.now(UTC) + timedelta(days=8)).replace(minute=0, second=0, microsecond=0)
    upcoming = _swap(_read("ocg_event_past.html"), STARTS, f'data-starts="{starts.isoformat()}"')
    upcoming = _swap(upcoming, 'data-registration-window-open="false"', 'data-registration-window-open="true"')
    cancelled = _swap(upcoming, 'data-canceled="false"', 'data-canceled="true"')
    definition = _definition().model_copy(update={"rate_limit_seconds": 0})
    requested = []

    def handler(request):
        requested.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        return httpx.Response(200, text=cancelled)

    service = DiscoveryService(
        lambda: Session(session.get_bind()), config, SourcesRegistry(sources=[definition]), organizers,
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), safety=URLSafety(_resolver),
    )
    (candidate,) = parse_event_page(upcoming, PAST_URL, definition.name, platform="ocg")
    assert await service._persist_candidate(session, candidate) == "eligible"
    try:
        result = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    session.expire_all()
    assert result == {"refreshed": 1, "errors": 0, "skipped": 0}
    event = session.exec(select(Event)).one()
    assert event.status == "rejected"
    assert event.registration_state == "cancelled"
    assert "Registration is cancelled" in event.relevance_reason
    assert requested == ["/robots.txt", "/cncf/group/52r68y4/event/quau4tx"]


@pytest.mark.parametrize("heading", ["<h1", "<h1 data-x"], ids=["empty_h1", "no_h1"])
def test_ocg_event_without_usable_heading_yields_no_candidates(heading):
    html = _read("ocg_event_past.html")
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.select_one("h1")
    if heading == "<h1":
        h1.clear()
    else:
        h1.decompose()
    assert _parse(str(soup)) == []


def test_ocg_non_decimal_digit_price_does_not_raise_and_is_not_paid_or_free():
    html = _swap(_read("ocg_event_past.html"), 'data-ticket-price-minor="0"', 'data-ticket-price-minor="\u00b2"')
    c = _one(html)
    assert c.price_text is None
    assert c.is_explicitly_paid is False


def test_ocg_malformed_group_href_leaves_organizer_none():
    html = _swap(
        _read("ocg_event_past.html"),
        'href="/cncf/group/52r68y4" hx-boost="true" hx-target="body">',
        'href="http://[x" hx-boost="true" hx-target="body">',
    )
    assert _one(html).organizer is None
