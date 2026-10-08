from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from eventfinder.domain import (
    admission_price_status,
    candidate_admission_statement,
    candidate_admission_status,
    free_admission_statement,
    has_required_admission_charge,
)
from eventfinder.models import DigestDelivery, DigestRun, Event, EventChange, EventSource
from eventfinder.policy import assess_candidate
from eventfinder.repository import pending_changes, revalidate_notification_policy, upsert_candidate
from eventfinder.sources import parse_event_page
from eventfinder.telegram import DigestService
from sqlmodel import select

NOW = datetime(2026, 10, 3, 4, tzinfo=UTC)


class NeverClassify:
    async def classify(self, candidate):
        raise AssertionError("Deterministic policy must run before AI")


@pytest.mark.parametrize(("price", "expected"), [
    (None, "not_stated"), ("", "not_stated"), ("Free Wi-Fi", "not_stated"),
    ("Free certification exam", "not_stated"), ("Free registration", "not_stated"),
    ("not free admission", "not_stated"), ("no free entry", "not_stated"),
    ("Free registration but tickets must be purchased", "paid"),
    ("Free entry; paid workshop pass", "paid"), ("₹499", "paid"),
    ("Free; 50", "paid"), ("Free; INR 0", "free"),
    (0, "free"), (0.0, "free"), ("EUR 0.00", "free"), ("0 INR", "free"),
    ("Free", "free"), ("Free admission", "free"), ("No entry fee", "free"),
    ("The event is free", "free"), ("The event is free of cost", "free"),
    ("The event is free of cos", "free"),
    ("The event is free?", "not_stated"), ("Attendance is free?", "not_stated"),
    ("Free admission?", "not_stated"), ("Admission is free?", "not_stated"),
])
def test_precise_admission_evidence(price, expected):
    assert admission_price_status(price) == expected


@pytest.mark.parametrize("description", [
    "The event is free.", "The event is free of cost.", "This event is free of charge.",
    "The workshop is free to attend.", "This meetup is free.", "The talk is free.",
    "The session is free of cost.", "This conference is free to attend.",
    "The hackathon is free.", "The buildathon is free.", "Attendance is free.",
    "1. RSVP opens soon\n2. The event is free of cos\n3. Waitlisted participants will be notified.",
    "Notes 1. RSVP opens soon 2. The event is free of cos 3. Waitlisted participants will be notified.",
])
@pytest.mark.asyncio
async def test_event_description_supplies_precise_admission_evidence(candidate, config, organizers, description):
    candidate.price_text = None
    candidate.description = "Technical AI engineering workshop. " + description
    assert free_admission_statement(candidate.description)
    assert (await assess_candidate(candidate, config, organizers)).status == "eligible"


@pytest.mark.parametrize("description", [
    "The event is not free.", "The event isn't free.", "The event is never free.",
    "If the event is free, we will attend.", "We cannot confirm that the event is free.",
    "The event is free Wi-Fi and snacks.",
    "The event is free for the first 50 tickets.", "The event is free as a trial.",
    "The event is free with a raffle win.", "The event is free of defects.",
    "Attendance is free for the first 50 tickets.",
    "Other event is free.", "Free certification exam.", "Free registration. Tickets TBD.",
    "The event is free?", "The workshop is free to attend?", "Attendance is free?",
])
@pytest.mark.asyncio
async def test_unverified_description_does_not_supply_admission(candidate, config, organizers, description):
    candidate.price_text = None
    candidate.description = "Technical AI engineering workshop. " + description
    result = await assess_candidate(candidate, config, organizers, NeverClassify())
    assert result.status == "needs_review"
    assert result.reason == "Free admission is not verified"


@pytest.mark.asyncio
@pytest.mark.parametrize("negation", ["The event is not free.", "This is not a free event.",
                                      "The workshop is not free.", "Attendance is not free."])
async def test_explicit_event_negation_blocks_conflicting_free_price(candidate, config, organizers, negation):
    candidate.description = "Technical AI engineering workshop. " + negation
    assert (await assess_candidate(candidate, config, organizers)).status == "needs_review"


@pytest.mark.asyncio
async def test_student_only_free_statement_remains_blocked(candidate, config, organizers):
    candidate.price_text = None
    candidate.description = "Technical AI workshop. The event is free for students only."
    assert (await assess_candidate(candidate, config, organizers)).status == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", [
    "The event is free. Members only.", "The event is free for members only.",
    "The workshop is free for members only.", "The event is free. Members-only attendance.",
])
async def test_membership_restriction_blocks_free_event(candidate, config, organizers, restriction):
    candidate.price_text = None
    candidate.description = "Technical AI workshop. " + restriction
    assessment = await assess_candidate(candidate, config, organizers, NeverClassify())
    assert assessment.status == "rejected"
    assert assessment.reason == "Explicitly incompatible eligibility"


@pytest.mark.asyncio
@pytest.mark.parametrize("url_key", ["url", "eventUrl", "event_url", "permalink", "link"])
@pytest.mark.parametrize("matching", [False, True])
async def test_nested_admission_statement_checks_event_identity_before_metadata(
    candidate, config, organizers, url_key, matching
):
    candidate.price_text = None
    observed_url = candidate.canonical_url if matching else "https://events.example.test/other"
    candidate.evidence.facts = {"merged_observations": [{"facts": {
        "parser": "json_ld", "admission_statement": "The event is free.",
        "event": {url_key: observed_url, "description": "The event is free."},
    }}]}
    assert candidate_admission_status(candidate) == ("free" if matching else "not_stated")
    assert bool(candidate_admission_statement(candidate)) is matching
    assessment = await assess_candidate(candidate, config, organizers, NeverClassify())
    assert assessment.status == ("eligible" if matching else "needs_review")


@pytest.mark.asyncio
@pytest.mark.parametrize("price", ["The event is free?", "Free admission?", "Admission is free?"])
async def test_question_only_price_does_not_qualify_or_notify(session, candidate, config, organizers, price):
    candidate.price_text = price
    candidate.description = "Technical AI engineering workshop. The event is free?"
    assert free_admission_statement(candidate.description) is None
    assessment = await assess_candidate(candidate, config, organizers, NeverClassify())
    assert assessment.status == "needs_review"
    event, _, _ = upsert_candidate(session, candidate, assessment, config)
    assert event.price_status == "not_stated"

    class Sender:
        async def send(self, body):
            raise AssertionError("Question-only price cannot establish free admission")

    assert (await DigestService(lambda: session, Sender()).send_daily_digest())["status"] == "silent"


def _bangpypers_node():
    return {
        "@type": "Event", "name": "Python Meetup X Functional Programming India",
        "url": "https://www.meetup.com/bangpypers/events/312819339/",
        "description": "Agenda: TBD\nVenue : EPAM System Bengaluru\nNote\n\n"
                       "1. RSVP opens 3 weeks before the even\n2. The event is free of cos\n"
                       "3. Waitlisted participants will receive confirmation notification about a day before the even",
        "startDate": "2026-10-24T10:30:00+05:30", "endDate": "2026-10-24T14:00:00+05:30",
        "location": {"name": "EPAM Systems Bangalore", "address": {"addressLocality": "Bengaluru"}},
        "organizer": {"name": "BangPypers - Bangalore Python Users Group"},
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "registrationStatus": "waitlist",
    }


@pytest.mark.asyncio
async def test_bangpypers_description_survives_parsing_storage_and_mock_digest(session, config, organizers):
    node = _bangpypers_node()
    html = f"""<script type='application/ld+json'>{json.dumps(node)}</script>
    <main><h1>{node['name']}</h1><dl><dt>Starts</dt><dd>2026-10-24 10:30 IST</dd>
    <dt>Venue</dt><dd>EPAM Systems Bengaluru</dd></dl>
    <aside><h2>Sponsors</h2><p>Paid meetup fees for the old term: INR 1500</p></aside></main>"""
    candidate = parse_event_page(html, node["url"], "meetup_bengaluru", NOW, platform="meetup")[0]
    assert candidate.price_text == "The event is free of cos"
    assessment = await assess_candidate(candidate, config, organizers, NeverClassify(), now=NOW)
    assert assessment.status == "eligible"
    event, _, _ = upsert_candidate(session, candidate, assessment, config)
    assert event.status == "eligible"
    assert event.price_status == "free"
    assert session.exec(select(EventSource)).one().evidence["admission_statement"] == "The event is free of cos"

    class Sender:
        bodies = []

        async def send(self, body):
            self.bodies.append(body)
            return "fixture"

    sender = Sender()
    assert (await DigestService(lambda: session, sender).send_daily_digest(now=NOW))["status"] == "sent"
    assert len(sender.bodies) == 1
    assert node["name"] in sender.bodies[0]
    assert "Free" in sender.bodies[0]


def test_nested_legacy_description_verifies_free_without_page_wide_inference(session, config):
    event, _ = _saved(session, "Python Meetup", "Free")
    event.price_status = "not_stated"
    session.add(event)
    session.add(EventSource(event_id=event.id, source_name="meetup", source_url=event.canonical_url,
                            evidence={"parser": "meetup:semantic_labels", "merged_observations": [
                                {"facts": {"parser": "json_ld", "event": {
                                    **_bangpypers_node(), "url": event.canonical_url,
                                }}},
                                {"facts": {"parser": "meetup:semantic_labels"}},
                            ]}))
    session.commit()
    assert revalidate_notification_policy(session, config, NOW) == {"checked": 1, "needs_review": 0, "rejected": 0}
    assert event.price_status == "free"
    assert len(pending_changes(session, NOW)) == 1


@pytest.mark.parametrize("url_key", ["url", "eventUrl", "event_url", "permalink", "link"])
@pytest.mark.parametrize("proof", ["statement", "admission_price", "price", "offers", "free_flag"])
@pytest.mark.parametrize("matching", [False, True])
def test_legacy_admission_observations_require_same_event_identity(session, config, url_key, proof, matching):
    event, _ = _saved(session, "Python Meetup", "Free")
    observed_url = event.canonical_url if matching else "https://example.test/other"
    node = {"@type": "Event", url_key: observed_url}
    facts = {"parser": "json_ld", "event": node}
    if proof == "statement":
        facts["admission_statement"] = "The event is free."
        node["description"] = "The event is free."
    elif proof == "admission_price":
        facts["admission_price"] = "Free admission"
    elif proof == "price":
        node["price"] = 0
    elif proof == "offers":
        node["offers"] = {"price": 0}
    else:
        node["isAccessibleForFree"] = True
    session.add(EventSource(event_id=event.id, source_name="meetup", source_url=event.canonical_url,
                            evidence={"parser": "meetup:semantic_labels", "merged_observations": [
                                {"facts": facts},
                            ]}))
    session.commit()
    assert revalidate_notification_policy(session, config, NOW)["needs_review"] == int(not matching)
    assert bool(pending_changes(session, NOW)) is matching


@pytest.mark.parametrize("price", ["The event is free?", "Free admission?", "Admission is free?"])
def test_legacy_question_only_admission_is_unverified(session, config, price):
    event, _ = _saved(session, "Python Meetup", price)
    event.description = "Technical Python meetup. The event is free?"
    session.add(event)
    session.add(EventSource(event_id=event.id, source_name="meetup", source_url=event.canonical_url,
                            evidence={"parser": "meetup:semantic_labels", "admission_price": price,
                                      "admission_statement": "The event is free?"}))
    session.commit()
    assert revalidate_notification_policy(session, config, NOW)["needs_review"] == 1
    assert event.price_status == "not_stated"
    assert pending_changes(session, NOW) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("charge", [
    "Tickets must be purchased for INR 1500.", "Registration fee applies.",
    "A mandatory donation of INR 500 is required to attend.",
    "Optional donations are welcome, but a mandatory donation of INR 500 is required to attend.",
    "A donation is required to attend: INR 500.",
    "You must make a donation of INR 500 to attend.",
])
async def test_event_free_statement_cannot_override_required_payment(candidate, config, organizers, charge):
    candidate.price_text = None
    candidate.description = "Technical AI workshop. The event is free. " + charge
    assert (await assess_candidate(candidate, config, organizers, NeverClassify())).status == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize("donation", [
    "An optional donation of INR 500 is welcome.",
    "Voluntary donations of INR 500 are appreciated.",
    "A donation of INR 500 is not required to attend.",
    "A required donation of INR 0 is necessary to attend.",
    "RSVP is required to attend, donations are welcome.",
    "You must register to attend and donations are welcome.",
    "A donation is required to attend: INR 0.",
])
async def test_free_attendance_allows_optional_or_zero_donations(candidate, config, organizers, donation):
    candidate.price_text = None
    candidate.description = "Technical AI workshop. The event is free. " + donation
    assert not has_required_admission_charge(candidate.description)
    assert (await assess_candidate(candidate, config, organizers, NeverClassify())).status == "eligible"


@pytest.mark.asyncio
@pytest.mark.parametrize("restriction", [
    "Members only.", "A mandatory donation of INR 500 is required to attend.",
])
async def test_saved_free_event_restrictions_cannot_reach_mock_digest(session, config, restriction):
    event, _ = _saved(session)
    event.description = "Technical AI workshop. The event is free. " + restriction
    session.add(event)
    session.commit()
    assert revalidate_notification_policy(session, config, NOW)["rejected"] == 1
    assert pending_changes(session, NOW) == []

    class Sender:
        async def send(self, body):
            raise AssertionError("Restricted or paid attendance must not reach Telegram")

    assert (await DigestService(lambda: session, Sender()).send_daily_digest(now=NOW))["status"] == "silent"


@pytest.mark.parametrize("text", [
    "Tickets must be purchased for INR 1500.", "Admission fee required.",
    "Entry fee applies.", "Registration fee is payable.", "Registration fee: INR 1500.",
    "Admission costs INR 1500", "General admission: $20", "Paid admission",
    "Ticket price: $20", "Ticket fee applies", "Tickets are paid",
])
def test_required_attendance_charges_conflict_with_free(text):
    assert has_required_admission_charge(text)
    assert admission_price_status("Free admission; " + text) == "paid"


@pytest.mark.parametrize("text", [
    "We will review and confirm your registration 3 days before the event (i.e : 07th Oct 2026)",
    "Registration is required 2 days before the event.",
    "Tickets are required at 10 am.",
    "registration 10:30 am", "Registration: 10 am", "Entry from 6 pm",
    "Registration 07/10/2026", "Registration 2026-10-07", "Tickets 7 Oct",
    "Tickets: 50 seats", "Tickets: 1,000 seats", "Registration: 100 spots only",
    "Entry 18 years and above", "Registration is 2 weeks before the event",
    "registration 1st come first served", "tickets 07th Oct",
    "registration limited to 100 attendees", "registration opens at 10 AM",
    "registration closes 48 hours before",
    "Tickets 15 dec", "Registration 10.30 a.m.", "Entry 7 may 2026",
])
def test_bare_times_dates_and_counts_after_admission_nouns_are_not_charges(text):
    assert not has_required_admission_charge(text)
    assert admission_price_status("Free admission; " + text) == "free"


@pytest.mark.parametrize("text", [
    "Registration fee 500", "Registration fee: 500", "Tickets ₹499", "Tickets 499/-",
    "Entry: Rs 200", "Registration: INR 1500", "Admission 250 rupees", "Ticket price 1,500",
    "Registration fee is required: 300", "Registration is required: 300", "Tickets 500",
    "Tickets 1,500", "Tickets 1999", "Tickets 500 per person", "Tickets 500 may sell out",
    "Ticket price: 2 day pass 999", "Registration fee is required 2 days before the event",
    "Tickets: 1-day pass INR 999", "Entry 6 pm onwards, Rs 200 cover",
    "Registration 3 days before, 499/- payable",
    "Early bird tickets 499 - Oct 15", "Tickets 1500 - May 31", "Tickets 1,500 - Dec 31",
    "Entry 200 may 2026", "Tickets 500 may, however, sell out", "Passes: 2 days, 15 €",
    "Tickets: 3 days, Rupees 500", "Tickets: 2 days, Rs: 500",
    "Registration: 9 am - 10 am, Rs: 200 on spot",
    "Contribution Rs 500, registration 3 days before the event",
    "Registration 3 days before, fee 500", "Entry 6 pm onwards, cover charge 200",
    "Registration 3 days before, pay 500 at the venue", "Registration 10 am, payment 500 on spot",
    "Tickets: 2 days, pricing 1500", "Tickets 7 Oct, priced 499", "Tickets: 50 seats, deposit 500",
    "Tickets: 2 days, buy for 999", "Tickets: 2 days, chargeable 500", "Tickets 50 may sell out",
])
def test_bare_amounts_after_admission_nouns_stay_paid(text):
    assert has_required_admission_charge(text)
    assert admission_price_status("Free admission; " + text) == "paid"


def test_admission_charge_scan_is_linear_on_whitespace_runs():
    started = time.perf_counter()
    has_required_admission_charge("tickets 5" + " " * 20000 + "x")
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize("text", [
    "Entry: Rs. 200", "Registration fee: Rs. 500", "Registration: 9 am - 10 am, Rs. 200 on spot",
    "A mandatory donation of Rs. 500 is required to attend.",
])
def test_rs_abbreviation_period_does_not_split_the_amount(candidate, text):
    assert has_required_admission_charge(text)
    candidate.description = "Technical AI workshop. " + text
    assert candidate_admission_status(candidate) == "paid"


@pytest.mark.asyncio
@pytest.mark.parametrize("description", [
    "Technical AI talk. No registration fee applies.",
    "Technical AI talk. Admission fee is waived.",
    "Technical AI talk. Entry fee is not required.",
    "Technical AI talk. Registration fee: INR 0.",
    "Technical AI talk. Tickets are required to attend.",
    "Technical AI talk. Registration fee is required: INR 0.",
    "Technical AI talk. Admission is required for attendees.",
    "Technical AI talk. Ticket price: $0.",
    "Technical AI hackathon with prizes worth INR 1500.",
    "Technical AI talk. Optional certification exam registration fee: INR 1500.",
    "Technical AI talk. Speaker studied ticket fees for INR 1500 travel bookings.",
])
async def test_free_talk_incidental_money_and_no_fee_contexts_are_allowed(candidate, config, organizers, description):
    candidate.description = description
    assert not has_required_admission_charge(description)
    assert (await assess_candidate(candidate, config, organizers)).status == "eligible"


@pytest.mark.asyncio
@pytest.mark.parametrize("charge", ["Tickets must be purchased for INR 1500.",
                                    "Admission costs INR 1500", "General admission: $20", "Paid admission"])
async def test_schema_free_claim_cannot_override_event_description_tickets(config, organizers, charge):
    node = {"@type": "Event", "name": "Bengaluru AI Workshop",
            "startDate": "2026-10-10T10:00:00+05:30", "location": {"name": "Bengaluru"},
            "isAccessibleForFree": True, "description": "Technical AI workshop. " + charge}
    html = f"<script type='application/ld+json'>{json.dumps(node)}</script>"
    candidate = parse_event_page(html, "https://example.test/event", "fixture")[0]
    assert candidate.price_text == "Free admission"
    assert candidate.evidence.facts["event"]["description"] == node["description"]
    assessment = await assess_candidate(candidate, config, organizers, NeverClassify(), now=NOW)
    assert assessment.status == "rejected"
    assert assessment.reason == "Explicitly paid admission"


@pytest.mark.asyncio
@pytest.mark.parametrize("null_offer", [False, True])
async def test_rootconf_null_price_is_not_free(config, organizers, null_offer):
    node = {"@type": "Event", "name": "Rootconf 2026 Annual Conference",
            "description": "Platforms for AI, and AI for Platforms",
            "startDate": "2026-11-13T09:00:00+05:30", "endDate": "2026-11-14T18:00:00+05:30",
            "location": {"@type": "Place", "address": {"addressLocality": "Bengaluru"}},
            "organizer": {"name": "Rootconf"}}
    if null_offer:
        node["offers"] = {"@type": "Offer", "price": None, "priceCurrency": "INR"}
    candidate = parse_event_page(f"<script type='application/ld+json'>{json.dumps(node)}</script>",
                                 "https://hasgeek.com/rootconf/2026/", "hasgeek", NOW)[0]
    assert candidate.price_text is None
    result = await assess_candidate(candidate, config, organizers, NeverClassify(), now=NOW)
    assert result.status == "needs_review"
    assert result.reason == "Free admission is not verified"


@pytest.mark.asyncio
async def test_nvidia_certification_feed_entries_rejected_before_ai(config, organizers):
    candidates = parse_event_page(Path("tests/fixtures/nvidia_webinars.json").read_text(),
                                  "https://www.nvidia.com/feed.json", "nvidia_developer", NOW,
                                  platform="nvidia_webinar")
    assert len(candidates) == 2
    for candidate in candidates:
        assessment = await assess_candidate(candidate, config, organizers, NeverClassify(), now=NOW)
        assert assessment.status == "rejected"
        assert assessment.reason == "Excluded event category"


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["Bangalore Tech Mixer", "NVIDIA Certification Roadmap", "AI exam prep"])
async def test_excluded_promotions_remain_excluded_even_when_free(candidate, config, organizers, title):
    candidate.title, candidate.description = title, "Meet fellow technology professionals and make connections"
    result = await assess_candidate(candidate, config, organizers, NeverClassify())
    assert result.status == "rejected"


@pytest.mark.asyncio
async def test_incidental_credential_and_networking_do_not_exclude_technical_talk(candidate, config, organizers):
    candidate.title = "AI engineering talk and networking mixer"
    candidate.description = "Technical talk with live coding. Speaker holds an NVIDIA certification."
    assert (await assess_candidate(candidate, config, organizers)).status == "eligible"


@pytest.mark.asyncio
@pytest.mark.parametrize("free", [False, True])
async def test_social_schema_and_stale_series_are_not_upcoming_occurrences(config, organizers, candidate, free):
    node = {"@type": "SocialEvent", "name": "Bangalore Tech Mixer and Social (Tech / AI / Data / IT)",
            "description": "Join us at our TECH MIXER AND SOCIAL for afterwork drinks, networking with tech / IT workers and connect with others in tech",
            "startDate": "2026-04-24T19:00:00+05:30", "endDate": "2026-12-25T22:00:00+05:30",
            "location": {"name": "Bangalore"}, "organizer": {"name": "Bangalore Tech Social"}}
    if free:
        node["offers"] = {"price": 0}
    parsed = parse_event_page(f"<script type='application/ld+json'>{json.dumps(node)}</script>",
                              "https://www.eventbrite.com/e/mixer", "eventbrite", NOW)[0]
    assert (await assess_candidate(parsed, config, organizers, NeverClassify(), now=NOW)).status == "rejected"
    parsed.title, parsed.description = "Tech Evening", "Meet fellow practitioners"
    assert (await assess_candidate(parsed, config, organizers, NeverClassify(), now=NOW)).status == "rejected"
    candidate.starts_at, candidate.ends_at = NOW - timedelta(days=170), NOW + timedelta(days=90)
    assert (await assess_candidate(candidate, config, organizers, now=NOW)).status == "needs_review"
    candidate.starts_at, candidate.ends_at = NOW - timedelta(days=2), NOW + timedelta(days=1)
    assert (await assess_candidate(candidate, config, organizers, now=NOW)).status == "eligible"


@pytest.mark.parametrize("incidental", ["Free Wi-Fi available", "Free certification discount", "Free entry for another event"])
def test_semantic_page_body_does_not_prove_admission(incidental):
    html = f"""<main><h1>AI Workshop</h1><dl><dt>Starts</dt><dd>2026-10-10 10:00 IST</dd>
    <dt>Venue</dt><dd>Bengaluru</dd></dl><p>{incidental}</p></main>"""
    assert parse_event_page(html, "https://example.test/event", "fixture")[0].price_text is None


def test_sidebar_price_does_not_prove_current_event_admission():
    html = """<main><h1>AI Workshop</h1><dl><dt>Starts</dt><dd>2026-10-10 10:00 IST</dd>
    <dt>Venue</dt><dd>Bengaluru</dd></dl></main>
    <aside><h2>Unrelated event</h2><dl><dt>Cost</dt><dd>Free</dd></dl></aside>"""
    assert parse_event_page(html, "https://example.test/event", "fixture")[0].price_text is None


@pytest.mark.parametrize("wrapper", ["main", "div"])
@pytest.mark.parametrize("unrelated", ["aside", "nav", "footer", "section class='related-events'", "section class='event-card'"])
def test_nested_sidebar_and_generic_page_other_event_prices_are_unknown(wrapper, unrelated):
    end_tag = unrelated.split()[0]
    html = f"""<{wrapper}><h1>AI Workshop</h1><dl><dt>Starts</dt><dd>2026-10-10 10:00 IST</dd>
    <dt>Venue</dt><dd>Bengaluru</dd></dl><section>Admission details pending</section>
    <{unrelated}><h2>Other event</h2><dl><dt>Cost</dt><dd>Free</dd></dl></{end_tag}></{wrapper}>"""
    candidate = parse_event_page(html, "https://example.test/event", "fixture")[0]
    assert candidate.price_text is None
    assert candidate.evidence.facts["admission_price"] is None


@pytest.mark.parametrize(("value", "expected"), [(True, "Free admission"), (False, None), ("true", None)])
def test_event_specific_schema_free_flag(value, expected):
    node = {"@type": "Event", "name": "AI Workshop", "startDate": "2026-10-10T10:00:00Z",
            "isAccessibleForFree": value}
    html = f"<script type='application/ld+json'>{json.dumps(node)}</script>"
    assert parse_event_page(html, "https://example.test/event", "fixture")[0].price_text == expected


def _saved(session, title="AI Engineering Workshop", price="Free admission"):
    event = Event(canonical_url=f"https://example.test/{title.replace(' ', '-')}", normalized_key=title,
                  title=title, description="Technical content", starts_at=NOW + timedelta(days=8),
                  price_text=price, price_status="free" if price else "not_stated",
                  ai_provenance={"rationale": "is technical", "provider": "legacy"})
    session.add(event)
    session.flush()
    change = EventChange(event_id=event.id, change_type="new_event", new_value="qualified")
    session.add(change)
    session.commit()
    return event, change


def test_revalidation_holds_legacy_unknown_and_unproven_free_without_deleting_history(session, config):
    unknown, _ = _saved(session, "RootConf", None)
    mixer, _ = _saved(session, "Bangalore Tech Mixer", None)
    free, change = _saved(session, "Python Meetup", "Free")
    session.add(EventSource(event_id=free.id, source_name="meetup", source_url=free.canonical_url,
                            evidence={"parser": "meetup:semantic_labels"}))
    session.add(DigestRun(digest_date="2026-10-02", status="sent", event_change_ids=[change.id]))
    session.commit()
    result = revalidate_notification_policy(session, config, NOW)
    assert result == {"checked": 3, "needs_review": 2, "rejected": 1}
    assert unknown.status == free.status == "needs_review"
    assert free.price_status == "not_stated"
    assert mixer.status == "rejected"
    assert len(session.exec(select(EventChange)).all()) == 3
    assert session.exec(select(DigestRun)).one().status == "sent"
    assert pending_changes(session, NOW) == []


@pytest.mark.asyncio
async def test_sparse_observation_preserves_verified_free_but_demotes_legacy_unknown(session, candidate, config, organizers):
    candidate.starts_at = NOW + timedelta(days=8)
    event, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers, now=NOW))
    sparse = candidate.model_copy(deep=True)
    sparse.price_text = None
    upsert_candidate(session, sparse, await assess_candidate(sparse, config, organizers, now=NOW))
    assert event.status == "eligible"
    event.price_text, event.price_status = None, "not_stated"
    session.add(event)
    session.commit()
    upsert_candidate(session, sparse, await assess_candidate(sparse, config, organizers, now=NOW))
    assert event.status == "needs_review"


@pytest.mark.asyncio
async def test_sparse_reobservation_cannot_wash_away_legacy_page_wide_free(session, candidate, config, organizers):
    event, _ = _saved(session, "Python Meetup", "Free")
    event.canonical_url = candidate.canonical_url
    session.add(event)
    session.add(EventSource(event_id=event.id, source_name=candidate.source_name,
                            source_url=candidate.source_url, evidence={"parser": "meetup:semantic_labels"}))
    session.commit()
    candidate.price_text = None
    candidate.evidence.facts = {"parser": "json_ld", "event": {"name": candidate.title}}
    upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers), config)
    assert pending_changes(session, NOW) == []
    revalidate_notification_policy(session, config, NOW)
    assert event.status == "needs_review"
    assert event.price_status == "not_stated"


@pytest.mark.asyncio
@pytest.mark.parametrize("history", ["none", "suppressed", "sent", "legacy_sent", "legacy_partial"])
async def test_terminal_updates_require_successful_prior_notification(session, history):
    event, prior = _saved(session)
    prior.digested_at = NOW - timedelta(days=1)
    event.status, event.registration_state = "rejected", "closed"
    if history != "none":
        run = DigestRun(digest_date="2026-10-02", status="suppressed" if history == "suppressed" else "sent",
                        event_change_ids=[prior.id])
        session.add(run)
        session.flush()
        session.add(DigestDelivery(digest_run_id=run.id, chunk_index=0, body="PRIOR MESSAGE",
                                  event_change_ids=None if history.startswith("legacy") else [prior.id],
                                  sent_at=None if history == "suppressed" else NOW - timedelta(days=1)))
        if history == "legacy_partial":
            session.add(DigestDelivery(digest_run_id=run.id, chunk_index=1, body="UNSENT MESSAGE"))
    terminal = EventChange(event_id=event.id, change_type="registration_state", old_value="open", new_value="closed")
    session.add_all([event, prior, terminal])
    session.commit()

    class Sender:
        bodies = []

        async def send(self, body):
            self.bodies.append(body)
            return "fixture"

    sender = Sender()
    result = await DigestService(lambda: session, sender).send_daily_digest(now=NOW)
    if history in {"sent", "legacy_sent"}:
        assert result["status"] == "sent"
        assert len(sender.bodies) == 1
        assert "Registration: closed" in sender.bodies[0]
    else:
        assert sender.bodies == []
        assert terminal.digested_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("run_status", ["pending", "partial", "failed"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_saved_retry_revalidates_bad_rows_and_preserves_sent_history(session, run_status, legacy):
    bad, bad_change = _saved(session, "Bangalore Tech Mixer")
    good, good_change = _saved(session)
    run = DigestRun(digest_date="2026-10-02", status=run_status,
                    event_change_ids=[bad_change.id, good_change.id])
    session.add(run)
    session.flush()
    history = DigestDelivery(digest_run_id=run.id, chunk_index=0, body="SENT HISTORY",
                             event_change_ids=[], sent_at=NOW - timedelta(days=1))
    retry = DigestDelivery(digest_run_id=run.id, chunk_index=1, body="FROZEN BAD MIXER",
                           event_change_ids=None if legacy else [bad_change.id, good_change.id])
    session.add_all([history, retry])
    session.commit()

    class Sender:
        bodies = []

        async def send(self, body):
            self.bodies.append(body)
            return "1"

    sender = Sender()
    result = await DigestService(lambda: session, sender).send_daily_digest(now=NOW)
    assert result["status"] == "sent"
    assert len(sender.bodies) == 1
    assert "AI Engineering Workshop" in sender.bodies[0]
    assert "Mixer" not in sender.bodies[0]
    assert history.body == "SENT HISTORY"
    assert history.sent_at is not None
    assert bad_change.digested_at is None
    assert (await DigestService(lambda: session, sender).send_daily_digest(now=NOW + timedelta(days=1)))["status"] == "silent"
    assert len(sender.bodies) == 1


@pytest.mark.asyncio
async def test_all_invalid_retry_is_suppressed_truthfully(session):
    _, change = _saved(session, "NVIDIA Certification Roadmap", None)
    run = DigestRun(digest_date="2026-10-02", status="partial", event_change_ids=[change.id])
    session.add(run)
    session.flush()
    delivery = DigestDelivery(digest_run_id=run.id, chunk_index=0, body="OLD CERTIFICATION",
                              event_change_ids=[change.id])
    session.add(delivery)
    session.commit()

    class Sender:
        async def send(self, body):
            raise AssertionError("Policy-invalid frozen content must not be sent")

    result = await DigestService(lambda: session, Sender()).send_daily_digest(now=NOW)
    assert result["status"] == run.status == "suppressed"
    assert result["sent"] == 0
    assert delivery.sent_at is None
    assert change.digested_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["description_charge", "nested_social"])
async def test_saved_policy_contradictions_are_rejected_before_retry(session, invalid):
    event, change = _saved(session, "Bengaluru AI Meetup" if invalid == "nested_social" else "AI Engineering Workshop")
    if invalid == "description_charge":
        event.description = "Technical AI talk. Tickets must be purchased for INR 1500."
    else:
        event.description = "Meet fellow practitioners"
        session.add(EventSource(event_id=event.id, source_name="fixture", source_url=event.canonical_url,
            evidence={"parser": "generic:semantic_labels", "admission_price": "Free admission",
                      "merged_observations": [{"facts": {"parser": "json_ld", "event": {"@type": "SocialEvent"}}},
                                              {"facts": {"parser": "generic:semantic_labels", "admission_price": "Free admission"}}]}))
    session.add(event)
    run = DigestRun(digest_date="2026-10-02", status="partial", event_change_ids=[change.id])
    session.add(run)
    session.flush()
    delivery = DigestDelivery(digest_run_id=run.id, chunk_index=0, body="OLD INVALID BODY",
                              event_change_ids=[change.id])
    session.add(delivery)
    session.commit()
    assert pending_changes(session, NOW) == []

    class Sender:
        async def send(self, body):
            raise AssertionError("Contradictory source evidence must block saved retries")

    assert (await DigestService(lambda: session, Sender()).send_daily_digest(now=NOW))["status"] == "suppressed"
    assert event.status == "rejected"
    assert delivery.sent_at is None
    assert change.digested_at is None


@pytest.mark.asyncio
async def test_invalid_failed_carry_cannot_starve_new_valid_events_across_days(session):
    invalid = [_saved(session, f"NVIDIA Certification {index}", None)[1] for index in range(10)]
    run = DigestRun(digest_date="2026-10-02", status="failed", event_change_ids=[change.id for change in invalid])
    session.add(run)
    session.flush()
    session.add(DigestDelivery(digest_run_id=run.id, chunk_index=0, body="OLD CERTIFICATION DIGEST",
                              event_change_ids=[change.id for change in invalid], attempt_count=3))
    _, good = _saved(session)

    class Sender:
        def __init__(self):
            self.bodies = []

        async def send(self, body):
            self.bodies.append(body)
            return "1"

    sender = Sender()
    service = DigestService(lambda: session, sender)
    assert (await service.send_daily_digest(now=NOW))["status"] == "sent"
    assert len(sender.bodies) == 1
    assert "AI Engineering Workshop" in sender.bodies[0]
    assert "Certification" not in sender.bodies[0]
    assert good.digested_at is not None
    assert all(change.digested_at is None for change in invalid)
    assert run.status == "suppressed"
    assert (await service.send_daily_digest(now=NOW + timedelta(days=1)))["status"] == "silent"
    assert len(sender.bodies) == 1
