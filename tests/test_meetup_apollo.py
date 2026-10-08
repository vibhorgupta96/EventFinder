from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from eventfinder.config import SourceDefinition, SourcesRegistry
from eventfinder.domain import (
    MEETUP_NO_FEE_TEXT,
    admission_price_status,
    candidate_admission_status,
    free_only_from_meetup_fee_settings,
    mentions_payment_terms,
)
from eventfinder.models import Event
from eventfinder.policy import assess_candidate
from eventfinder.repository import upsert_candidate
from eventfinder.service import DiscoveryService, _observed_event_provenance
from eventfinder.sources import parse_event_page
from eventfinder.urls import URLSafety
from sqlmodel import Session, select

FIXTURES = Path("tests/fixtures")
NOW = datetime(2026, 10, 8, 6, tzinfo=UTC)
GROUP_URL = "https://www.meetup.com/bangpypers/events/"
EVENT_URL = "https://www.meetup.com/bangpypers/events/101/"


def _group_page() -> str:
    return (FIXTURES / "meetup_group_events.html").read_text(encoding="utf-8")


def _parse(html: str, url: str = GROUP_URL, platform: str | None = "meetup"):
    return parse_event_page(html, url, "meetup_bangpypers", NOW, platform)


def _by_id(candidates):
    return {candidate.evidence.raw_id: candidate for candidate in candidates}


def _candidate(raw_id: str):
    return _by_id(_parse(_group_page()))[raw_id]


def test_no_meetup_fee_is_free_but_never_beats_a_listed_fee():
    assert admission_price_status(MEETUP_NO_FEE_TEXT) == "free"
    assert admission_price_status("No Meetup fee; INR 500") == "paid"
    assert admission_price_status("INR 500; No Meetup fee") == "paid"
    assert admission_price_status("Meetup fee is unknown") == "not_stated"


def test_free_only_from_meetup_fee_settings_requires_sole_free_evidence(candidate):
    fee_only = candidate.model_copy(update={"price_text": MEETUP_NO_FEE_TEXT})
    assert free_only_from_meetup_fee_settings(fee_only) is True
    stated = candidate.model_copy(
        update={"price_text": MEETUP_NO_FEE_TEXT, "description": "The event is free of cost."}
    )
    assert candidate_admission_status(stated) == "free"
    assert free_only_from_meetup_fee_settings(stated) is False
    assert free_only_from_meetup_fee_settings(candidate) is False


def test_group_page_yields_only_in_scope_active_or_cancelled_events():
    candidates = _parse(_group_page())
    assert sorted(_by_id(candidates)) == [str(i) for i in range(101, 109)]
    assert {c.evidence.facts["parser"] for c in candidates} == {"meetup:apollo"}


def test_fee_settings_null_is_recorded_as_free_evidence_without_member_data():
    candidate = _candidate("101")
    assert candidate.price_text == MEETUP_NO_FEE_TEXT
    assert candidate_admission_status(candidate) == "free"
    assert candidate.evidence.facts["meetup_fee_settings"] is None
    assert candidate.evidence.facts["admission_evidence"] == "meetup_fee_settings_null"
    assert candidate.evidence.raw_id == "101"
    assert candidate.format.value == "in_person"
    assert candidate.city == "Bengaluru"
    assert candidate.registration_state.value == "open"
    serialized = json.dumps(candidate.evidence.facts)
    assert "eventHosts" not in serialized and "rsvps" not in serialized


@pytest.mark.asyncio
async def test_listed_fee_is_paid_and_rejected(config, organizers):
    candidate = _candidate("102")
    assert candidate.is_explicitly_paid is True
    assert candidate_admission_status(candidate) == "paid"
    assessment = await assess_candidate(candidate, config, organizers, classifier=None, now=NOW)
    assert (assessment.status, assessment.reason) == ("rejected", "Explicitly paid admission")
    assert candidate.evidence.facts["meetup_fee_settings"]["amount"] == 500


@pytest.mark.asyncio
async def test_missing_fee_field_stays_unverified(config, organizers):
    candidate = _candidate("103")
    assert candidate.price_text is None
    assert candidate_admission_status(candidate) == "not_stated"
    assert "meetup_fee_settings" not in candidate.evidence.facts
    assessment = await assess_candidate(candidate, config, organizers, classifier=None, now=NOW)
    assert (assessment.status, assessment.reason) == ("needs_review", "Free admission is not verified")


def test_network_event_null_fee_is_not_free_and_description_fee_is_paid():
    assert candidate_admission_status(_candidate("104")) == "not_stated"
    assert candidate_admission_status(_candidate("105")) == "paid"


def test_registration_state_comes_from_meetup_status_and_rsvp_settings():
    candidates = _by_id(_parse(_group_page()))
    assert candidates["106"].registration_state.value == "cancelled"
    assert candidates["107"].registration_state.value == "closed"
    assert candidates["108"].registration_state.value == "unknown"


def test_private_group_and_empty_state_yield_nothing():
    html = _group_page().replace('"isPrivate": false', '"isPrivate": true')
    assert _parse(html) == []
    empty = (
        '<meta property="og:title" content="BangPypers"><script id="__NEXT_DATA__" type="application/json">'
        '{"props": {"pageProps": {"__APOLLO_STATE__": {"ROOT_QUERY": {}}}}}</script>'
    )
    assert _parse(empty) == []


def test_apollo_state_is_ignored_for_other_platforms():
    candidates = _parse(_group_page(), platform="official")
    assert all(c.evidence.facts["parser"] != "meetup:apollo" for c in candidates)


def test_detail_page_merges_semantic_labels_with_apollo_evidence():
    html = (FIXTURES / "meetup_group_event_detail.html").read_text(encoding="utf-8")
    candidates = _parse(html, EVENT_URL)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.price_text == MEETUP_NO_FEE_TEXT
    assert candidate_admission_status(candidate) == "free"
    assert candidate.evidence.facts["parser"] == "meetup:apollo"


def test_detail_event_outside_the_page_group_is_ignored():
    html = (FIXTURES / "meetup_group_event_detail.html").read_text(encoding="utf-8")
    other = _parse(html, "https://www.meetup.com/other-group/events/101/")
    assert all(c.evidence.facts["parser"] != "meetup:apollo" for c in other)


async def _assessed(candidate, config, organizers):
    return await assess_candidate(candidate, config, organizers, classifier=None, now=NOW)


@pytest.mark.asyncio
async def test_stored_paid_status_is_not_overridden_by_fee_setting_only_evidence(session, config, organizers):
    fee_free = _candidate("101")
    seed = fee_free.model_copy(deep=True, update={"price_text": None})
    seed.evidence.facts.pop("admission_evidence")
    event, _, _ = upsert_candidate(session, seed, await _assessed(seed, config, organizers), config)
    assert event.status == "needs_review"
    event.price_status = "paid"
    session.add(event)
    session.commit()
    updated, _, _ = upsert_candidate(session, fee_free, await _assessed(fee_free, config, organizers), config)
    session.refresh(updated)
    assert updated.price_status == "paid"
    assert updated.price_text is None
    assert updated.status == "rejected"
    assert updated.relevance_reason == "Explicitly paid admission"


@pytest.mark.asyncio
async def test_unknown_stored_price_accepts_fee_setting_evidence(session, config, organizers):
    fee_free = _candidate("101")
    seed = fee_free.model_copy(deep=True, update={"price_text": None})
    seed.evidence.facts.pop("admission_evidence")
    event, _, _ = upsert_candidate(session, seed, await _assessed(seed, config, organizers), config)
    assert event.price_status == "not_stated"
    updated, _, _ = upsert_candidate(session, fee_free, await _assessed(fee_free, config, organizers), config)
    session.refresh(updated)
    assert updated.price_status == "free"
    assert updated.status == "eligible"


@pytest.mark.asyncio
async def test_stored_paid_yields_to_a_stated_free_admission_statement(session, config, organizers):
    fee_free = _candidate("101")
    seed = fee_free.model_copy(deep=True, update={"price_text": None})
    seed.evidence.facts.pop("admission_evidence")
    event, _, _ = upsert_candidate(session, seed, await _assessed(seed, config, organizers), config)
    event.price_status = "paid"
    session.add(event)
    session.commit()
    stated = fee_free.model_copy(deep=True, update={"description": "The event is free of cost."})
    updated, _, _ = upsert_candidate(session, stated, await _assessed(stated, config, organizers), config)
    session.refresh(updated)
    assert updated.price_status == "free"


async def _public_resolver(_hostname: str) -> list[str]:
    return ["93.184.216.34"]


def _page(*, fee: bool | None, start: datetime) -> str:
    """Inline Meetup detail page; fee True=key missing, False=null fee setting."""
    node = {
        "__typename": "Event", "id": "101", "title": "Python Meetup Refresh Check",
        "eventUrl": EVENT_URL, "description": "Python and AI engineering talks and hands-on sessions.",
        "group": {"__ref": "Group:1"}, "venue": {"__ref": "Venue:1"},
        "dateTime": start.isoformat(), "endTime": (start + timedelta(hours=2)).isoformat(),
        "isOnline": False, "eventType": "PHYSICAL", "status": "ACTIVE", "rsvpState": "JOIN_OPEN",
        "rsvpSettings": {"rsvpsClosed": False}, "isNetworkEvent": False,
    }
    if fee is False:
        node["feeSettings"] = None
    state = {
        "ROOT_QUERY": {'event({"id":"101"})': {"__ref": "Event:101"}},
        "Event:101": node,
        "Group:1": {"__typename": "Group", "id": "1", "name": "BangPypers", "urlname": "bangpypers", "isPrivate": False},
        "Venue:1": {"__typename": "Venue", "id": "1", "name": "Example Hall", "address": "1 Sample Road", "city": "Bengaluru"},
    }
    data = {"props": {"pageProps": {"__APOLLO_STATE__": state}}}
    return (f'<html><head><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}'
            "</script></head><body></body></html>")


def _definition() -> SourceDefinition:
    return SourceDefinition(
        name="meetup_bangpypers", adapter="public_page", platform="meetup", url=GROUP_URL,
        allowed_domains=["meetup.com"], rate_limit_seconds=0, cadence_hours=12,
    )


def _service(session, config, organizers, definition, html):
    def handler(request):
        text = "User-agent: *\nAllow: /\n" if request.url.path == "/robots.txt" else html
        return httpx.Response(200, text=text)

    return DiscoveryService(
        lambda: Session(session.get_bind()), config, SourcesRegistry(sources=[definition]),
        organizers, httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        safety=URLSafety(_public_resolver),
    )


async def _seeded_service(session, config, organizers, start):
    definition = _definition()
    service = _service(session, config, organizers, definition, _page(fee=False, start=start))
    seed = parse_event_page(
        _page(fee=True, start=start), EVENT_URL, definition.name, platform="meetup",
    )[0]
    assert await service._persist_candidate(session, seed) == "needs_review"
    event = session.exec(select(Event)).one()
    assert event.relevance_reason == "Free admission is not verified"
    return service, definition, event


@pytest.mark.asyncio
async def test_refresh_promotes_review_row_from_meetup_fee_setting(session, config, organizers):
    start = datetime.now(UTC) + timedelta(days=8)
    service, definition, event = await _seeded_service(session, config, organizers, start)
    try:
        result = await service.refresh_known_events()
    finally:
        await service.client.aclose()
    session.expire_all()
    updated = session.get(Event, event.id)
    assert result == {"refreshed": 1, "errors": 0, "skipped": 0}
    assert (updated.status, updated.price_status, updated.price_text) == ("eligible", "free", MEETUP_NO_FEE_TEXT)


@pytest.mark.asyncio
async def test_refresh_keeps_stored_paid_row_rejected(session, config, organizers):
    start = datetime.now(UTC) + timedelta(days=8)
    service, definition, event = await _seeded_service(session, config, organizers, start)
    event.price_status = "paid"
    session.add(event)
    session.commit()
    try:
        await service.refresh_known_events()
    finally:
        await service.client.aclose()
    session.expire_all()
    updated = session.get(Event, event.id)
    assert (updated.status, updated.price_status) == ("rejected", "paid")
    assert updated.relevance_reason == "Explicitly paid admission"


def test_meetup_apollo_is_trusted_observed_event_provenance():
    from eventfinder.models import EventSource

    source = EventSource(event_id=1, source_name="meetup_bangpypers", source_url=EVENT_URL,
                         evidence={"parser": "meetup:apollo"})
    assert _observed_event_provenance(source, _definition(), EVENT_URL) is True


def _node_page(extra=None, extra_state=None, drop=(), description="Python talks.", private=False):
    """Inline Meetup detail page with a configurable Event node."""
    node = {
        "__typename": "Event", "id": "101", "title": "Python Meetup", "eventUrl": EVENT_URL,
        "description": description, "group": {"__ref": "Group:1"}, "venue": {"__ref": "Venue:1"},
        "dateTime": "2026-11-11T10:30:00+05:30", "endTime": "2026-11-11T13:00:00+05:30",
        "isOnline": False, "eventType": "PHYSICAL", "status": "ACTIVE", "rsvpState": "JOIN_OPEN",
        "rsvpSettings": {"rsvpsClosed": False}, "isNetworkEvent": False, "feeSettings": None,
    }
    node.update(extra or {})
    for key in drop:
        node.pop(key, None)
    state = {
        "ROOT_QUERY": {'event({"id":"101"})': {"__ref": "Event:101"}},
        "Event:101": node,
        "Group:1": {"__typename": "Group", "id": "1", "name": "BangPypers", "urlname": "bangpypers",
                    "isPrivate": private},
        "Venue:1": {"__typename": "Venue", "id": "1", "name": "Hall", "address": "1 Road", "city": "Bengaluru"},
    }
    state.update(extra_state or {})
    data = {"props": {"pageProps": {"__APOLLO_STATE__": state}}}
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script>'


def _apollo(html: str):
    return [c for c in _parse(html, EVENT_URL) if c.evidence.facts["parser"] == "meetup:apollo"]


@pytest.mark.parametrize("state", [{}, {"EventFeeSettings:x": None}], ids=["dangling_ref", "null_target"])
@pytest.mark.asyncio
async def test_unresolved_fee_ref_is_not_a_null_fee(state, config, organizers):
    html = _node_page({"feeSettings": {"__ref": "EventFeeSettings:x"}}, state)
    (candidate,) = _apollo(html)
    assert candidate.price_text is None
    assert candidate_admission_status(candidate) == "not_stated"
    assert candidate.evidence.facts.get("meetup_fee_settings") == {"unresolved_ref": True}
    assert "admission_evidence" not in candidate.evidence.facts
    assessment = await assess_candidate(candidate, config, organizers, classifier=None, now=NOW)
    assert (assessment.status, assessment.reason) == ("needs_review", "Free admission is not verified")


def test_resolved_fee_ref_with_amount_is_still_paid():
    html = _node_page({"feeSettings": {"__ref": "EventFeeSettings:x"}},
                      {"EventFeeSettings:x": {"amount": 500, "currency": "INR", "accepts": "CASH"}})
    (candidate,) = _apollo(html)
    assert candidate.is_explicitly_paid is True
    assert candidate_admission_status(candidate) == "paid"


PAYMENT_DESCRIPTIONS = [
    "This is a paid event.",
    "Paid workshop, INR 2000",
    "Fee: Rs 200 per person",
    "Price: INR 499",
    "Cost: ₹300 (includes lunch)",
    "Charges: INR 500",
    "Participation fee: 300 INR",
    "Please pay Rs. 100 at the venue",
    "₹500 per person for food",
    "Buy a ticket on Konfhub",
    "Payments accepted at the venue",
    "You will be charged 200 at the door",
    "Pricing: 500 per head",
    "Paying attendees only",
    "This is a ticketed event",
    "Collecting 300 rupees for lunch",
    "Donation of 100 requested",
    "Charging 300 for lunch",
    "Contributions welcome: 200 per head",
    "This is a chargeable workshop.",
    "Entry is not free.",
    "Entry not free",
    "Grab your pass on Konfhub.",
    "Passes available on Townscript.",
    "Refundable deposit of 500 to confirm your seat.",
    "Purchase your seat at https://townscript.com/e/x",
    "A nominal amount of 200 will be collected for food.",
    "Registration amount: 299 (includes lunch)",
]


@pytest.mark.parametrize("description", PAYMENT_DESCRIPTIONS)
@pytest.mark.asyncio
async def test_null_fee_with_payment_terms_is_not_free(description, config, organizers):
    (candidate,) = _apollo(_node_page(description=description))
    assert candidate.price_text is None
    assert candidate_admission_status(candidate) != "free"
    assert candidate.evidence.facts["admission_evidence"] == "meetup_fee_settings_null_suppressed_by_payment_terms"
    assessment = await assess_candidate(candidate, config, organizers, classifier=None, now=NOW)
    assert assessment.status != "eligible"


def test_payment_terms_in_the_title_also_suppress_fee_evidence():
    (candidate,) = _apollo(_node_page({"title": "Python Workshop: Rs 300 entry"}))
    assert candidate.price_text is None
    assert candidate_admission_status(candidate) != "free"


def test_clean_technical_description_with_null_fee_stays_free():
    (candidate,) = _apollo(_node_page(description="Python and AI engineering talks, lightning demos and networking."))
    assert candidate.price_text == MEETUP_NO_FEE_TEXT
    assert candidate_admission_status(candidate) == "free"


def test_free_statement_with_null_fee_is_free_via_statement_path():
    (candidate,) = _apollo(_node_page(description="The event is free of cost."))
    assert candidate_admission_status(candidate) == "free"


@pytest.mark.parametrize(
    ("text", "expected"),
    [("Entry ₹ 200", True), ("200 INR", True), ("$5", True), ("USD 10", True), ("Rs.100", True),
     ("fees apply", True),
     ("Payments accepted at the venue", True), ("You will be charged 200 at the door", True),
     ("Pricing: 500 per head", True), ("Paying attendees only", True), ("This is a ticketed event", True),
     ("Collecting 300 rupees for lunch", True), ("Donation of 100 requested", True),
     ("Charging 300 for lunch", True), ("Contributions welcome: 200 per head", True), ("500/-", True),
     ("This is a chargeable workshop.", True), ("Entry is not free.", True), ("Entry not free", True), ("Grab your pass on Konfhub.", True), ("Passes available on Townscript.", True), ("Refundable deposit of 500 to confirm your seat.", True), ("Purchase your seat at https://townscript.com/e/x", True), ("A nominal amount of 200 will be collected for food.", True), ("Registration amount: 299 (includes lunch)", True),
     ("Lunch: 250 per person", False), ("Bring a laptop", False), ("Prices of tea", True), ("worship paid", True),
     ("Trapezoid shaped hall, rsvp soon", False), ("Python meetup", False)],
)
def test_mentions_payment_terms(text, expected):
    assert mentions_payment_terms(text) is expected


@pytest.mark.parametrize(
    "event_id",
    ["111", "112", "113", "114"],
    ids=["group_node_mismatch", "slug_url_mismatch", "before_connection_only", "foreign_host"],
)
def test_provenance_guards_each_reject_exactly_their_event(event_id):
    assert event_id not in _by_id(_parse(_group_page()))


def test_private_group_reached_via_detail_event_path_yields_nothing():
    assert _apollo(_node_page(private=True)) == []


@pytest.mark.parametrize("drop", [("isNetworkEvent",), ()], ids=["missing_key", "null_value"])
def test_missing_or_null_network_flag_gives_no_free_evidence(drop):
    extra = {} if drop else {"isNetworkEvent": None}
    (candidate,) = _apollo(_node_page(extra, drop=drop))
    assert candidate.price_text is None
    assert candidate_admission_status(candidate) == "not_stated"


def test_non_meetup_host_with_matching_slug_is_ignored():
    html = _node_page({"eventUrl": "https://evil.example/bangpypers/events/101/"})
    assert _apollo(html) == []


@pytest.mark.parametrize("fee", ["FREE", [], 0, 500, True], ids=["str", "list", "zero", "int", "bool"])
def test_non_dict_non_null_fee_settings_are_not_a_null_fee(fee):
    (candidate,) = _apollo(_node_page({"feeSettings": fee}))
    assert candidate.price_text is None
    assert candidate_admission_status(candidate) == "not_stated"
    assert candidate.evidence.facts["meetup_fee_settings"] == {"unparsed_type": type(fee).__name__}
    assert "admission_evidence" not in candidate.evidence.facts
