from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from eventfinder.config import SourceDefinition, SourcesRegistry
from eventfinder.models import Event, EventChange, EventSource
from eventfinder.repository import pending_changes
from eventfinder.service import DiscoveryService
from eventfinder.sources import parse_event_page
from eventfinder.urls import URLSafety
from sqlmodel import Session, select


async def _public_resolver(_hostname: str) -> list[str]:
    return ["93.184.216.34"]


def _html(*nodes: dict) -> str:
    return f'<script type="application/ld+json">{json.dumps(list(nodes))}</script>'


def _node(url: str, organizer: str, *, timezone: str = "Asia/Kolkata") -> dict:
    future = (datetime.now(UTC) + timedelta(days=8)).astimezone(ZoneInfo(timezone))
    return {
        "@type": "Event", "name": "AI engineering workshop", "url": url,
        "description": "Technical AI systems and LLM engineering workshop",
        "organizer": organizer,
        "startDate": future.replace(hour=18, minute=0, second=0, microsecond=0, tzinfo=None).isoformat(),
        "eventAttendanceMode": "https://schema.org/OnlineEventAttendanceMode",
        "registrationStatus": "open", "price": "Free",
    }


def _service(session, config, organizers, definition, html):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        return httpx.Response(200, text="User-agent: *\nAllow: /\n" if request.url.path == "/robots.txt" else html)

    service = DiscoveryService(
        lambda: Session(session.get_bind()), config, SourcesRegistry(sources=[definition]),
        organizers, httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        safety=URLSafety(_public_resolver),
    )
    return service, requested


async def _seed(service, session, definition, node, source_url):
    candidate = parse_event_page(
        _html(node), source_url, definition.name, platform=definition.platform,
        tz=definition.source_timezone, dayfirst=definition.date_dayfirst,
    )[0]
    assert await service._persist_candidate(session, candidate) == "eligible"
    return session.exec(select(Event)).one(), candidate


async def _seed_admission_review(service, session, definition, url, name):
    node = _node(url, "Microsoft Reactor")
    node.pop("price")
    node["name"] = "AI engineering workshop " + name
    candidate = parse_event_page(_html(node), url, definition.name, platform=definition.platform)[0]
    assert await service._persist_candidate(session, candidate) == "needs_review"
    event = session.exec(select(Event).where(Event.canonical_url == url)).one()
    assert event.relevance_reason == "Free admission is not verified"
    return event, node, candidate


@pytest.mark.asyncio
async def test_review_refresh_promotes_only_matching_event_and_keeps_precise_provenance(
    session, config, organizers
):
    definition = SourceDefinition(name="microsoft_reactor", adapter="public_page", platform="official",
                                  url="https://events.microsoft.com/", rate_limit_seconds=0)
    url = "https://events.microsoft.com/review"
    service, requested = _service(session, config, organizers, definition, "")
    event, node, _ = await _seed_admission_review(service, session, definition, url, "review")
    node["description"] += ". The event is free of cost."
    incidental = {**node, "url": "https://events.microsoft.com/unrelated", "name": "Other AI workshop"}
    await service.client.aclose()
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: (
        requested.append(str(request.url)) or httpx.Response(200, text=(
            "User-agent: *\nAllow: /\n" if request.url.path == "/robots.txt" else _html(node, incidental)
        ))
    )))
    service.robots_policy.client = service.client
    try:
        result = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    session.expire_all()
    updated = session.get(Event, event.id)
    assert result == {"refreshed": 1, "errors": 0, "skipped": 0}
    assert updated.status == "eligible"
    assert updated.price_status == "free"
    assert updated.price_text == "The event is free of cost"
    assert [item.id for item in session.exec(select(Event)).all()] == [event.id]
    source = session.exec(select(EventSource)).one()
    assert source.evidence["admission_statement"] == "The event is free of cost"
    assert source.evidence["admission_review_refresh_at"]
    assert pending_changes(session)
    assert all("unrelated" not in request for request in requested)


@pytest.mark.asyncio
async def test_refresh_keeps_eligible_priority_and_selects_due_reviews_before_cap(
    session, config, organizers
):
    definition = SourceDefinition(name="microsoft_reactor", adapter="public_page", platform="official",
                                  url="https://events.microsoft.com/", rate_limit_seconds=0, cadence_hours=24)
    service, requested = _service(session, config, organizers, definition, "")
    eligible_url = "https://events.microsoft.com/eligible"
    eligible, _ = await _seed(service, session, definition, _node(eligible_url, "Microsoft Reactor"), eligible_url)
    cooling, cooling_node, _ = await _seed_admission_review(service, session, definition,
                                                          "https://events.microsoft.com/cooling", "cooling")
    due, due_node, _ = await _seed_admission_review(service, session, definition,
                                                  "https://events.microsoft.com/due", "due")
    never, never_node, _ = await _seed_admission_review(service, session, definition,
                                                      "https://events.microsoft.com/never", "never")
    for event, attempted in [(cooling, datetime.now(UTC)), (due, datetime.now(UTC) - timedelta(days=2))]:
        source = session.exec(select(EventSource).where(EventSource.event_id == event.id)).one()
        source.evidence = {**source.evidence, "admission_review_refresh_at": attempted.isoformat()}
        session.add(source)
    session.commit()
    nodes = [_node(eligible_url, "Microsoft Reactor"), cooling_node, due_node, never_node]
    await service.client.aclose()
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: (
        requested.append(str(request.url)) or httpx.Response(200, text=(
            "User-agent: *\nAllow: /\n" if request.url.path == "/robots.txt" else _html(*nodes)
        ))
    )))
    service.robots_policy.client = service.client
    try:
        first = await service.refresh_known_events(limit=5)
        second = await service.refresh_known_events(limit=5)
    finally:
        await service.client.aclose()
    assert first == second == {"refreshed": 2, "errors": 0, "skipped": 0}
    detail_requests = [url for url in requested if not url.endswith("robots.txt")]
    assert detail_requests == [eligible_url, never.canonical_url, eligible_url, due.canonical_url]
    assert cooling.canonical_url not in requested
    session.expire_all()
    assert session.get(Event, eligible.id).status == "eligible"
    assert all(session.get(Event, item.id).status == "needs_review" for item in [cooling, due, never])


@pytest.mark.asyncio
@pytest.mark.parametrize("denial", ["robots", "captcha", "http_error"])
async def test_review_refresh_failures_cool_down_and_sparse_observations_retain_attempt(
    session, config, organizers, denial
):
    definition = SourceDefinition(name="microsoft_reactor", adapter="public_page", platform="official",
                                  url="https://events.microsoft.com/", rate_limit_seconds=0, cadence_hours=24)
    service, requested = _service(session, config, organizers, definition, "")
    event, _, candidate = await _seed_admission_review(service, session, definition,
                                                      "https://events.microsoft.com/failure", "failure")

    def handler(request):
        requested.append(str(request.url))
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\n" + ("Disallow: /\n" if denial == "robots" else "Allow: /\n"))
        if denial == "http_error":
            return httpx.Response(403)
        return httpx.Response(200, text="<h1>Verify you are human</h1>")

    await service.client.aclose()
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service.robots_policy.client = service.client
    try:
        first = await service.refresh_known_events()
        attempts = len(requested)
        await service._persist_candidate(session, candidate)
        second = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    assert first == {"refreshed": 0, "errors": 1, "skipped": 0}
    assert second == {"refreshed": 0, "errors": 0, "skipped": 0}
    assert len(requested) == attempts
    session.expire_all()
    assert session.get(Event, event.id).status == "needs_review"
    assert session.exec(select(EventSource)).one().evidence["admission_review_refresh_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["paid", "closed", "location", "topic", "window"])
async def test_review_refresh_reassesses_all_other_eligibility_gates(session, config, organizers, invalid):
    definition = SourceDefinition(name="microsoft_reactor", adapter="public_page", platform="official",
                                  url="https://events.microsoft.com/", rate_limit_seconds=0)
    service, _ = _service(session, config, organizers, definition, "")
    event, node, _ = await _seed_admission_review(service, session, definition,
                                                "https://events.microsoft.com/invalid", "invalid")
    node["description"] += ". The event is free."
    if invalid == "paid":
        node["description"] += " Registration fee applies."
    elif invalid == "closed":
        node["registrationStatus"] = "closed"
    elif invalid == "location":
        node["eventAttendanceMode"] = "https://schema.org/OfflineEventAttendanceMode"
        node["location"] = {"name": "Mumbai"}
    elif invalid == "topic":
        node["name"], node["description"] = "Pottery meetup", "Pottery practice. The event is free."
    else:
        node["startDate"] = (datetime.now(UTC) + timedelta(days=240)).isoformat()
    await service.client.aclose()
    service.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, text="User-agent: *\nAllow: /\n" if request.url.path == "/robots.txt" else _html(node)
    )))
    service.robots_policy.client = service.client
    try:
        result = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    session.expire_all()
    assert result == {"refreshed": 1, "errors": 0, "skipped": 0}
    assert session.get(Event, event.id).status != "eligible"
    assert pending_changes(session) == []


@pytest.mark.asyncio
async def test_refresh_retains_source_timezone_attribution_and_ignores_incidental_events(
    session, config, organizers, monkeypatch
):
    definition = SourceDefinition(
        name="microsoft_reactor", adapter="public_page", platform="official",
        url="https://developer.microsoft.com/en-us/reactor/",
        allowed_domains=["developer.microsoft.com", "events.microsoft.com"],
        allowed_registration_domains=["developer.microsoft.com", "events.microsoft.com"],
        source_timezone="America/Los_Angeles", date_dayfirst=False, rate_limit_seconds=0,
        max_pages=2, pagination_param="page", profile_url="https://developer.microsoft.com/en-us/reactor/",
    )
    url = "https://events.microsoft.com/event/ai-workshop"
    node = _node(url, "Microsoft Reactor", timezone=definition.source_timezone)
    incidental = {**node, "name": "Unrelated AI event", "url": "https://events.microsoft.com/event/unrelated"}
    service, requested = _service(session, config, organizers, definition, _html(node, incidental))
    event, initial = await _seed(service, session, definition, node, url)
    from eventfinder.sources import make_source

    observed = []

    def capture(refresh_definition, *args):
        observed.append(refresh_definition)
        return make_source(refresh_definition, *args)

    monkeypatch.setattr("eventfinder.service.make_source", capture)
    try:
        result = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    session.expire_all()
    updated = session.get(Event, event.id)
    assert result == {"refreshed": 1, "errors": 0, "skipped": 0}
    assert updated.starts_at == initial.starts_at
    assert initial.starts_at == datetime.fromisoformat(node["startDate"]).replace(tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(UTC)
    assert session.exec(select(Event)).all() == [updated]
    assert [change.change_type for change in session.exec(select(EventChange)).all()] == ["new_event"]
    source = session.exec(select(EventSource)).one()
    assert source.source_name == "microsoft_reactor"
    assert source.evidence["source_timezone"] == "America/Los_Angeles"
    assert observed[0].source_timezone == definition.source_timezone
    assert observed[0].date_dayfirst is False
    assert observed[0].platform == definition.platform
    assert observed[0].profile_url == definition.profile_url
    assert set(observed[0].allowed_domains) == set(definition.allowed_domains)
    assert set(observed[0].allowed_registration_domains) == set(definition.allowed_registration_domains)
    assert observed[0].max_pages == 1
    assert observed[0].max_detail_pages == 0
    assert url in requested
    assert all("unrelated" not in value and "page=" not in value for value in requested)


@pytest.mark.asyncio
async def test_refresh_retains_profile_trust_for_official_alias_on_multitenant_host(
    session, config, organizers
):
    definition = SourceDefinition(
        name="gdg_bengaluru", adapter="public_page", platform="meetup",
        url="https://www.meetup.com/gdg-bengaluru/", allowed_domains=["meetup.com"], rate_limit_seconds=0,
    )
    url = "https://www.meetup.com/gdg-bengaluru/events/310000001/"
    node = _node(url, "GDG Bengaluru")
    service, _ = _service(session, config, organizers, definition, _html(node))
    event, _ = await _seed(service, session, definition, node, url)
    generic = SourceDefinition(name="meetup_bengaluru", adapter="public_page", url="https://www.meetup.com/find/", allowed_domains=["meetup.com"], rate_limit_seconds=0)
    service.sources = SourcesRegistry(sources=[generic, definition])
    # An older generic cross-post must not replace a verified profile merely
    # because it appears first in the persisted provenance inventory.
    session.add(EventSource(
        event_id=event.id, source_name=generic.name, source_url="https://www.meetup.com/find/",
        evidence={"parser": "json_ld", "organizer_trust": "low"},
        observed_at=datetime.now(UTC) - timedelta(days=1),
    ))
    session.commit()
    try:
        result = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    session.expire_all()
    updated = session.get(Event, event.id)
    assert result == {"refreshed": 1, "errors": 0, "skipped": 0}
    assert updated.status == "eligible"
    assert updated.organizer_trust == "high"
    detail_source = session.exec(select(EventSource).where(EventSource.source_url == url)).one()
    assert detail_source.source_name == definition.name
    assert [change.change_type for change in session.exec(select(EventChange)).all()] == ["new_event"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["name", "source_url", "canonical_url", "parser", "disabled"])
async def test_refresh_skips_unverified_or_disabled_attribution(
    session, config, organizers, invalid
):
    definition = SourceDefinition(
        name="microsoft_reactor", adapter="public_page", url="https://events.microsoft.com/calendar", rate_limit_seconds=0,
    )
    url = "https://events.microsoft.com/event/ai-workshop"
    node = _node(url, "Microsoft Reactor")
    service, requested = _service(session, config, organizers, definition, _html(node))
    event, _ = await _seed(service, session, definition, node, url)
    source = session.exec(select(EventSource)).one()
    if invalid == "name":
        source.source_name = "known_event_refresh"
    elif invalid == "source_url":
        source.source_url = "https://unverified.example.test/pretend-official"
    elif invalid == "canonical_url":
        event.canonical_url = "https://unverified.example.test/event"
    elif invalid == "parser":
        source.evidence = {"organizer_trust": "high"}
    else:
        service.sources = SourcesRegistry(sources=[definition.model_copy(update={"enabled": False})])
    session.add_all([event, source])
    session.commit()
    try:
        result = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    assert result == {"refreshed": 0, "errors": 0, "skipped": 1}
    assert requested == []


@pytest.mark.asyncio
async def test_refresh_redirect_host_does_not_widen_registration_boundary(
    session, config, organizers
):
    definition = SourceDefinition(
        name="cncf_bengaluru", adapter="public_page", platform="official",
        url="https://community.cncf.io/cloud-native-bangalore/",
        allowed_domains=["community.cncf.io", "ocgroups.dev"], rate_limit_seconds=0,
    )
    url = "https://ocgroups.dev/cncf/group/52r68y4/event/ai-workshop"
    node = _node(url, "CNCF")
    service, requested = _service(session, config, organizers, definition, _html(node))
    event, _ = await _seed(service, session, definition, node, url)
    # The origin remains eligible for fetches, but the original source never
    # permitted its transport host as a displayable registration destination.
    event.registration_url = None
    session.add(event)
    session.commit()
    try:
        result = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    session.expire_all()
    assert result["refreshed"] == 1
    assert url in requested
    assert session.get(Event, event.id).registration_url is None


@pytest.mark.asyncio
async def test_refresh_search_provenance_reuses_explicit_result_boundary(
    session, config, organizers
):
    definition = SourceDefinition(
        name="microsoft_reactor", adapter="search", query="AI engineering workshops",
        allowed_domains=["events.microsoft.com"], source_timezone="America/Los_Angeles",
        date_dayfirst=False, rate_limit_seconds=0,
    )
    url = "https://events.microsoft.com/event/ai-workshop"
    node = _node(url, "Microsoft Reactor", timezone=definition.source_timezone)
    service, requested = _service(session, config, organizers, definition, _html(node))
    event, initial = await _seed(service, session, definition, node, url)
    try:
        result = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    session.expire_all()
    assert result == {"refreshed": 1, "errors": 0, "skipped": 0}
    assert session.get(Event, event.id).starts_at == initial.starts_at
    assert url in requested


@pytest.mark.asyncio
async def test_refresh_without_matching_known_candidate_reports_skip(
    session, config, organizers
):
    definition = SourceDefinition(name="microsoft_reactor", adapter="public_page", url="https://events.microsoft.com/calendar", rate_limit_seconds=0)
    url = "https://events.microsoft.com/event/ai-workshop"
    node = _node(url, "Microsoft Reactor")
    unrelated = {**node, "url": "https://events.microsoft.com/event/other", "name": "Other AI event"}
    service, _ = _service(session, config, organizers, definition, _html(unrelated))
    event, _ = await _seed(service, session, definition, node, url)
    try:
        result = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    assert result == {"refreshed": 0, "errors": 0, "skipped": 1}
    assert [item.id for item in session.exec(select(Event)).all()] == [event.id]


@pytest.mark.asyncio
async def test_nvidia_refresh_refetches_configured_feed_and_updates_only_known_identity(
    session, config, organizers, monkeypatch
):
    from eventfinder.sources import make_source

    feed_url = "https://www.nvidia.com/content/dam/en-zz/Solutions/about-nvidia/webinar/webinarJSONData.json"
    definition = SourceDefinition(
        name="nvidia_developer", adapter="public_page", platform="nvidia_webinar",
        url=feed_url, allowed_domains=["nvidia.com"],
        allowed_registration_domains=["nvidia.com"], rate_limit_seconds=0,
        source_timezone="America/Los_Angeles", date_dayfirst=False,
    )
    initial_start = datetime.now(UTC) + timedelta(days=8)
    row = {
        "eventId": 5503388, "title": "AI engineering workshop", "type": "Upcoming",
        "liveStartTimeInUTC": int(initial_start.timestamp() * 1000),
        "liveEndTimeInUTC": int((initial_start + timedelta(hours=1)).timestamp() * 1000),
        "eventAbstract": "<p>Technical AI systems and LLM engineering workshop.</p>",
        "mediaType": "Webcast",
    }
    updated_row = {**row, "liveStartTimeInUTC": row["liveStartTimeInUTC"] + 3600000, "liveEndTimeInUTC": row["liveEndTimeInUTC"] + 3600000}
    incidental_row = {**updated_row, "eventId": 5503389}
    feed = json.dumps({"data": [updated_row, incidental_row]})
    service, requested = _service(session, config, organizers, definition, feed)
    initial = parse_event_page(json.dumps({"data": [row]}), feed_url, definition.name, platform=definition.platform)[0]
    # Seed separately observed free admission; the feed supplies no price.
    initial.price_text = "Free admission"
    assert await service._persist_candidate(session, initial) == "eligible"
    event = session.exec(select(Event)).one()
    observed = []

    def capture(refresh_definition, *args):
        observed.append(refresh_definition)
        return make_source(refresh_definition, *args)

    monkeypatch.setattr("eventfinder.service.make_source", capture)
    try:
        result = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    session.expire_all()
    updated = session.get(Event, event.id)
    assert result == {"refreshed": 1, "errors": 0, "skipped": 0}
    assert updated.starts_at == initial.starts_at + timedelta(hours=1)
    assert updated.ends_at == initial.ends_at + timedelta(hours=1)
    assert updated.canonical_url == "https://www.nvidia.com/en-us/about-nvidia/webinar-portal/#/webinar/5503388"
    assert [item.id for item in session.exec(select(Event)).all()] == [event.id]
    assert set(requested) == {feed_url, "https://www.nvidia.com/robots.txt"}
    assert observed[0].url == feed_url
    assert observed[0].platform == "nvidia_webinar"
    assert observed[0].source_timezone == definition.source_timezone
    assert observed[0].date_dayfirst is False
    source = session.exec(select(EventSource)).one()
    assert source.source_name == definition.name
    assert source.source_url == feed_url
    assert source.raw_id == "5503388"
    assert source.evidence["parser"] == "nvidia_webinar_feed"
    assert source.evidence["event"] == updated_row
    assert updated.registration_url is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["source_url", "configured_url", "raw_id", "parser", "event_id"])
async def test_nvidia_refresh_requires_verified_feed_identity(
    session, config, organizers, invalid
):
    feed_url = "https://www.nvidia.com/content/dam/en-zz/Solutions/about-nvidia/webinar/webinarJSONData.json"
    definition = SourceDefinition(name="nvidia_developer", adapter="public_page", platform="nvidia_webinar", url=feed_url, allowed_domains=["nvidia.com"], rate_limit_seconds=0)
    future = datetime.now(UTC) + timedelta(days=8)
    row = {
        "eventId": 5503388, "title": "AI engineering workshop", "type": "Upcoming",
        "liveStartTimeInUTC": int(future.timestamp() * 1000),
        "eventAbstract": "Technical AI systems engineering workshop", "mediaType": "Webcast",
    }
    feed = json.dumps({"data": [row]})
    service, requested = _service(session, config, organizers, definition, feed)
    candidate = parse_event_page(feed, feed_url, definition.name, platform=definition.platform)[0]
    candidate.price_text = "Free admission"
    assert await service._persist_candidate(session, candidate) == "eligible"
    source = session.exec(select(EventSource)).one()
    if invalid == "source_url":
        source.source_url = "https://www.nvidia.com/other-feed.json"
    elif invalid == "configured_url":
        service.sources = SourcesRegistry(sources=[definition.model_copy(update={"url": "https://www.nvidia.com/other-feed.json"})])
    elif invalid == "raw_id":
        source.raw_id = "5503389"
    elif invalid == "parser":
        source.evidence = {**source.evidence, "parser": "json_ld"}
    else:
        source.evidence = {**source.evidence, "event": {**row, "eventId": 5503389}}
    session.add(source)
    session.commit()
    try:
        result = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    assert result == {"refreshed": 0, "errors": 0, "skipped": 1}
    assert requested == []


@pytest.mark.asyncio
async def test_nvidia_distinct_ids_with_identical_metadata_survive_discovery_and_refresh(
    session, config, organizers
):
    feed_url = "https://www.nvidia.com/content/dam/en-zz/Solutions/about-nvidia/webinar/webinarJSONData.json"
    definition = SourceDefinition(name="nvidia_developer", adapter="public_page", platform="nvidia_webinar", url=feed_url, allowed_domains=["nvidia.com"], rate_limit_seconds=0)
    future = datetime.now(UTC) + timedelta(days=8)
    row = {
        "eventId": 5503388, "title": "AI engineering workshop", "type": "Upcoming",
        "liveStartTimeInUTC": int(future.timestamp() * 1000),
        "eventAbstract": "Technical AI systems engineering workshop", "mediaType": "Webcast",
    }
    other = {**row, "eventId": 5503389}
    feed = json.dumps({"data": [row, other]})
    service, requested = _service(session, config, organizers, definition, feed)
    try:
        discovered = await service.run_discovery(force=True)
        refreshed = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    session.expire_all()
    assert discovered["fetched"] == 2
    assert discovered["accepted"] == 0
    assert discovered["review"] == 2
    assert refreshed == {"refreshed": 2, "errors": 0, "skipped": 0}
    events = session.exec(select(Event)).all()
    assert len(events) == 2
    assert len({event.normalized_key for event in events}) == 1
    assert {event.canonical_url for event in events} == {
        "https://www.nvidia.com/en-us/about-nvidia/webinar-portal/#/webinar/5503388",
        "https://www.nvidia.com/en-us/about-nvidia/webinar-portal/#/webinar/5503389",
    }
    sources = session.exec(select(EventSource)).all()
    assert {source.raw_id for source in sources} == {"5503388", "5503389"}
    assert len({source.event_id for source in sources}) == 2
    assert [change.change_type for change in session.exec(select(EventChange)).all()] == ["new_event", "new_event"]
    assert set(requested) == {feed_url, "https://www.nvidia.com/robots.txt"}
