from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from eventfinder.config import SourceDefinition, SourcesRegistry
from eventfinder.models import Event, EventChange, EventSource
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
    assert discovered["accepted"] == 2
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
