from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from eventfinder.domain import (
    EventCandidate,
    EventFormat,
    RegistrationState,
    SourceEvidence,
)
from eventfinder.models import Event, EventChange, EventSource
from eventfinder.policy import assess_candidate
from eventfinder.repository import (
    expire_past_events,
    list_events,
    normalized_event_key,
    pending_changes,
    upsert_candidate,
)
from sqlmodel import select


@pytest.mark.asyncio
async def test_cross_postings_dedupe_and_retain_provenance(session, candidate, config, organizers):
    assessment = await assess_candidate(candidate, config, organizers)
    event, changes, created = upsert_candidate(session, candidate, assessment)
    assert created is True
    assert [change.change_type for change in changes] == ["new_event"]

    cross_posting = candidate.model_copy(deep=True)
    cross_posting.canonical_url = "https://another.example.test/events/ai-systems"
    cross_posting.source_url = "https://another.example.test/events/ai-systems"
    cross_posting.source_name = "luma_bengaluru"
    same_event, changes, created = upsert_candidate(session, cross_posting, assessment)
    assert created is False
    assert same_event.id == event.id
    assert not changes
    sources = list(session.exec(select(EventSource).where(EventSource.event_id == event.id)).all())
    assert {source.source_name for source in sources} == {"gdg_bengaluru", "luma_bengaluru"}
    assert len(list_events(session, source="luma")) == 1


@pytest.mark.asyncio
async def test_cross_posting_differing_only_by_utm_param_and_host_case_dedupes(
    session, candidate, config, organizers
):
    """``EventCandidate`` normalizes ``canonical_url`` at construction time, so a
    cross-posting whose URL differs only by tracking-param noise or host case
    must still resolve to the same event via ``find_existing``'s exact
    ``canonical_url`` match, not a distinct row.
    """
    assessment = await assess_candidate(candidate, config, organizers)
    event, _, created = upsert_candidate(session, candidate, assessment)
    assert created is True

    # A model_copy() with a later plain attribute assignment would bypass the
    # ``strip_urls`` validator entirely, so this cross-posting is built via
    # the constructor (like the ``candidate`` fixture) to exercise the real
    # normalization path.
    cross_posting = EventCandidate(
        title=candidate.title,
        canonical_url="https://EVENTS.example.test/ai-systems?utm_source=newsletter",
        # A distinct source domain keeps this test isolated to canonical_url
        # dedupe via ``find_existing``; the ``utm_campaign``/host-case noise
        # still exercises the same ``normalize_url`` defensive path.
        source_url="https://LUMA.example.test/e/ai-systems?utm_campaign=weekly",
        source_name="luma_bengaluru",
        organizer=candidate.organizer,
        description=candidate.description,
        starts_at=candidate.starts_at,
        city=candidate.city,
        venue=candidate.venue,
        format=candidate.format,
        event_type=candidate.event_type,
        registration_state=candidate.registration_state,
        speakers=candidate.speakers,
        topics=candidate.topics,
        evidence=SourceEvidence(
            source_name="luma_bengaluru",
            source_url="https://LUMA.example.test/e/ai-systems?utm_campaign=weekly",
            observed_at=datetime.now(UTC),
        ),
    )
    assert cross_posting.canonical_url == candidate.canonical_url

    same_event, changes, created = upsert_candidate(session, cross_posting, assessment)
    assert created is False
    assert same_event.id == event.id
    assert not changes
    sources = list(session.exec(select(EventSource).where(EventSource.event_id == event.id)).all())
    assert {source.source_name for source in sources} == {"gdg_bengaluru", "luma_bengaluru"}


@pytest.mark.asyncio
async def test_meetup_recommendation_variant_finds_sparse_existing_row(
    session, candidate, config, organizers
):
    sparse = candidate.model_copy(deep=True)
    sparse.canonical_url = "https://www.meetup.com/python-bengaluru/events/310000001/?recId=abc"
    sparse.source_url = sparse.canonical_url
    sparse.starts_at = None
    sparse.venue = None
    sparse.city = None
    sparse.format = EventFormat.UNKNOWN
    assessment = await assess_candidate(candidate, config, organizers)
    event, _, created = upsert_candidate(session, sparse, assessment)
    assert created is True

    detailed = candidate.model_copy(deep=True)
    detailed.canonical_url = (
        "https://www.meetup.com/python-bengaluru/events/310000001/"
        "?searchId=xyz&eventOrigin=home_page"
    )
    detailed.source_url = detailed.canonical_url
    merged, _, created = upsert_candidate(session, detailed, assessment)

    assert created is False
    assert merged.id == event.id
    assert merged.starts_at == candidate.starts_at
    assert merged.venue == candidate.venue
    assert merged.canonical_url == sparse.canonical_url
    assert set(merged.source_urls) == {sparse.source_url, detailed.source_url}


@pytest.mark.asyncio
async def test_meetup_exact_sparse_url_updates_richer_historical_row(
    session, candidate, config, organizers
):
    sparse_url = "https://www.meetup.com/python-bengaluru/events/310000001/?recId=old"
    rich_url = (
        "https://www.meetup.com/python-bengaluru/events/310000001/"
        "?searchId=old&eventOrigin=search"
    )
    sparse = Event(
        canonical_url=sparse_url,
        normalized_key="old-sparse-key",
        title=candidate.title,
        status="needs_review",
    )
    rich = Event(
        canonical_url=rich_url,
        normalized_key="old-rich-key",
        title=candidate.title,
        organizer=candidate.organizer,
        description=candidate.description,
        starts_at=candidate.starts_at,
        venue=candidate.venue,
        city=candidate.city,
        format=candidate.format.value,
        registration_state=candidate.registration_state.value,
        status="eligible",
    )
    session.add_all([sparse, rich])
    session.commit()

    observation = candidate.model_copy(deep=True)
    observation.canonical_url = sparse_url  # exact URL must not win over richer evidence
    observation.source_url = (
        "https://www.meetup.com/python-bengaluru/events/310000001/?recSource=notification"
    )
    updated, changes, created = upsert_candidate(
        session, observation, await assess_candidate(observation, config, organizers)
    )

    assert created is False
    assert updated.id == rich.id
    assert updated.source_urls == [observation.source_url]
    assert changes == []
    assert session.exec(select(EventSource).where(EventSource.event_id == rich.id)).one().source_url == observation.source_url
    assert len(session.exec(select(Event)).all()) == 2
    assert session.exec(select(EventChange)).all() == []


@pytest.mark.asyncio
async def test_meetup_eligible_row_wins_over_richer_review_row(
    session, candidate, config, organizers
):
    eligible_url = "https://www.meetup.com/python-bengaluru/events/310000001/?recId=old"
    review_url = (
        "https://www.meetup.com/python-bengaluru/events/310000001/"
        "?searchId=old&eventOrigin=search"
    )
    eligible = Event(
        canonical_url=eligible_url,
        normalized_key="old-eligible-key",
        title=candidate.title,
        starts_at=candidate.starts_at,
        venue=candidate.venue,
        format=candidate.format.value,
        registration_state=candidate.registration_state.value,
        status="eligible",
    )
    richer_review = Event(
        canonical_url=review_url,
        normalized_key="old-review-key",
        title=candidate.title,
        organizer=candidate.organizer,
        description=candidate.description,
        starts_at=candidate.starts_at,
        ends_at=candidate.starts_at + timedelta(hours=2),
        venue=candidate.venue,
        city=candidate.city,
        format=candidate.format.value,
        registration_state=candidate.registration_state.value,
        status="needs_review",
    )
    session.add_all([eligible, richer_review])
    session.commit()

    observation = candidate.model_copy(deep=True)
    observation.canonical_url = review_url  # exact match must not promote a second eligible row
    observation.source_url = (
        "https://www.meetup.com/python-bengaluru/events/310000001/?recSource=notification"
    )
    updated, changes, created = upsert_candidate(
        session, observation, await assess_candidate(observation, config, organizers)
    )

    assert created is False
    assert updated.id == eligible.id
    assert updated.source_urls == [observation.source_url]
    assert changes == []
    assert richer_review.status == "needs_review"
    assert len(session.exec(select(Event).where(Event.status == "eligible")).all()) == 1
    assert session.exec(select(EventChange)).all() == []


@pytest.mark.asyncio
async def test_distinct_meetup_event_ids_do_not_merge_on_matching_metadata(
    session, candidate, config, organizers
):
    assessment = await assess_candidate(candidate, config, organizers)
    first = candidate.model_copy(deep=True)
    first.canonical_url = "https://www.meetup.com/python-bengaluru/events/310000001/?recId=abc"
    first.source_url = first.canonical_url
    event, _, _ = upsert_candidate(session, first, assessment)

    second = candidate.model_copy(deep=True)
    second.canonical_url = "https://www.meetup.com/python-bengaluru/events/310000002/?recId=abc"
    second.source_url = second.canonical_url
    distinct, _, created = upsert_candidate(session, second, assessment)

    assert created is True
    assert distinct.id != event.id


@pytest.mark.asyncio
async def test_material_registration_and_schedule_changes_are_immutable(session, candidate, config, organizers):
    assessment = await assess_candidate(candidate, config, organizers)
    event, _, _ = upsert_candidate(session, candidate, assessment)
    revised = candidate.model_copy(deep=True)
    revised.registration_state = RegistrationState.WAITLIST
    revised.starts_at = candidate.starts_at + timedelta(hours=1)
    updated, changes, _ = upsert_candidate(session, revised, assessment)
    assert updated.id == event.id
    assert {change.change_type for change in changes} == {"registration_state", "schedule"}
    stored_changes = list(session.exec(select(EventChange).where(EventChange.event_id == event.id)).all())
    assert len(stored_changes) == 3


@pytest.mark.asyncio
async def test_registration_url_tracking_variants_do_not_create_changes(
    session, candidate, config, organizers
):
    assessment = await assess_candidate(candidate, config, organizers)
    event, _, _ = upsert_candidate(session, candidate, assessment)
    first_url = "https://www.meetup.com/python-bengaluru/events/310000001/?recId=first"
    first = candidate.model_copy(deep=True)
    first.registration_url = first_url
    _, changes, _ = upsert_candidate(session, first, assessment)
    assert [change.change_type for change in changes] == ["registration_url"]
    assert changes[0].old_value is None

    tracking_url = (
        "https://www.meetup.com/python-bengaluru/events/310000001/"
        "?recSource=search&searchId=second&eventOrigin=home_page"
    )
    tracking = candidate.model_copy(deep=True)
    tracking.registration_url = tracking_url
    updated, changes, _ = upsert_candidate(session, tracking, assessment)
    assert changes == []
    assert updated.id == event.id
    assert updated.registration_url == tracking_url

    functional = candidate.model_copy(deep=True)
    functional.registration_url = tracking_url + "&ticket=vip"
    _, changes, _ = upsert_candidate(session, functional, assessment)
    assert [change.change_type for change in changes] == ["registration_url"]

    new_destination = candidate.model_copy(deep=True)
    new_destination.registration_url = "https://tickets.example.test/python-meetup"
    _, changes, _ = upsert_candidate(session, new_destination, assessment)
    assert [change.change_type for change in changes] == ["registration_url"]


@pytest.mark.asyncio
async def test_open_transition_is_observed_and_terminal_existing_event_is_persisted(
    session, candidate, config, organizers
):
    candidate.registration_state = RegistrationState.UNKNOWN
    assessment = await assess_candidate(candidate, config, organizers)
    event, _, _ = upsert_candidate(session, candidate, assessment)
    assert event is not None

    opened = candidate.model_copy(deep=True)
    opened.registration_state = RegistrationState.OPEN
    opened.evidence.observed_at = datetime(2026, 9, 23, tzinfo=UTC)
    updated, changes, _ = upsert_candidate(session, opened, await assess_candidate(opened, config, organizers))
    assert updated is not None
    assert updated.registration_opened_at is None
    assert updated.first_observed_open_at == opened.evidence.observed_at
    assert "registration_opened" in {change.change_type for change in changes}

    cancelled = opened.model_copy(deep=True)
    cancelled.registration_state = RegistrationState.CANCELLED
    terminal, changes, _ = upsert_candidate(
        session, cancelled, await assess_candidate(cancelled, config, organizers)
    )
    assert terminal is not None
    assert terminal.status == "rejected"
    assert terminal.registration_state == "cancelled"
    assert "registration_state" in {change.change_type for change in changes}
    assert any(
        change.change_type == "registration_state" and event.id == terminal.id
        for change, event in pending_changes(session)
    )


@pytest.mark.asyncio
async def test_first_seen_open_event_does_not_claim_an_opening_time(
    session, candidate, config, organizers
):
    candidate.registration_opened_at = None
    candidate.first_observed_open_at = None
    event, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    assert event is not None
    assert event.registration_state == "open"
    assert event.registration_opened_at is None
    assert event.first_observed_open_at is None


@pytest.mark.asyncio
async def test_rejected_new_candidate_is_not_stored_but_paid_update_is(session, candidate, config, organizers):
    candidate.is_explicitly_paid = True
    event, _, created = upsert_candidate(
        session, candidate, await assess_candidate(candidate, config, organizers)
    )
    assert event is None
    assert created is False

    candidate.is_explicitly_paid = False
    event, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    assert event is not None
    paid = candidate.model_copy(deep=True)
    paid.is_explicitly_paid = True
    paid.price_text = "INR 499"
    updated, changes, _ = upsert_candidate(session, paid, await assess_candidate(paid, config, organizers))
    assert updated is not None
    assert updated.status == "rejected"
    assert updated.price_status == "paid"
    assert "price" in {change.change_type for change in changes}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["paid", "unverified_online", "incompatible_eligibility"])
@pytest.mark.parametrize("initial_price", [None, "Free"])
async def test_lower_trust_rejection_preserves_eligible_event_and_provenance(
    session, candidate, config, organizers, reason, initial_price
):
    candidate.price_text = initial_price
    candidate.format = EventFormat.ONLINE
    candidate.city = None
    candidate.venue = None
    event, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    previous_reason = event.relevance_reason
    weaker = candidate.model_copy(deep=True)
    weaker.source_name = "luma_bengaluru"
    weaker.source_url = "https://lu.ma/unverified-crosspost"
    weaker.evidence = SourceEvidence(
        source_name=weaker.source_name,
        source_url=weaker.source_url,
        observed_at=datetime.now(UTC),
    )
    if reason == "paid":
        weaker.price_text = "INR 499"
        weaker.is_explicitly_paid = True
    elif reason == "incompatible_eligibility":
        weaker.eligibility_text = "Students only"
    assessment = await assess_candidate(weaker, config, organizers)
    assert assessment.status == "rejected"
    assert assessment.organizer_trust == "low"

    updated, changes, created = upsert_candidate(session, weaker, assessment)

    assert created is False
    assert updated.id == event.id
    assert updated.status == "eligible"
    assert updated.price_status == ("free" if initial_price else "not_stated")
    assert updated.price_text == initial_price
    assert updated.eligibility_text is None
    assert updated.organizer_trust == "high"
    assert updated.relevance_reason == previous_reason
    assert changes == []
    evidence = session.exec(select(EventSource).where(EventSource.source_url == weaker.source_url)).one().evidence
    assert evidence["organizer_trust"] == "low"
    if reason == "paid":
        assert evidence["unmerged_facts"]["price_text"] == "INR 499"
        assert evidence["unmerged_facts"]["is_explicitly_paid"] is True
    elif reason == "incompatible_eligibility":
        assert evidence["unmerged_facts"]["eligibility_text"] == "Students only"


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_price", [None, "Free"])
async def test_higher_trust_paid_rejection_remains_authoritative(session, candidate, config, organizers, initial_price):
    candidate.organizer = "Unknown organizer"
    candidate.source_name = "luma_bengaluru"
    candidate.price_text = initial_price
    event, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    assert event.organizer_trust == "low"
    paid = candidate.model_copy(deep=True)
    paid.organizer = "Google Developer Groups Bengaluru"
    paid.source_name = "gdg_bengaluru"
    paid.source_url = "https://events.example.test/official-update"
    paid.price_text = "INR 499"
    paid.is_explicitly_paid = True
    assessment = await assess_candidate(paid, config, organizers)
    assert assessment.organizer_trust == "high"

    updated, changes, _ = upsert_candidate(session, paid, assessment)

    assert updated.id == event.id
    assert updated.status == "rejected"
    assert updated.price_status == "paid"
    assert updated.price_text == "INR 499"
    assert {change.change_type for change in changes} >= {"price", "lifecycle"}


@pytest.mark.asyncio
async def test_lower_trust_rejected_end_time_cannot_expire_future_event(
    session, candidate, config, organizers
):
    candidate.price_text = "Free"
    assert candidate.ends_at is None
    event, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    weaker = candidate.model_copy(deep=True)
    weaker.source_name = "luma_bengaluru"
    weaker.source_url = "https://lu.ma/rejected-past-end"
    weaker.ends_at = datetime.now(UTC) - timedelta(days=1)
    weaker.price_text = "INR 499"
    weaker.is_explicitly_paid = True
    assessment = await assess_candidate(weaker, config, organizers)
    assert assessment.status == "rejected"
    assert assessment.organizer_trust == "low"

    updated, changes, _ = upsert_candidate(session, weaker, assessment)

    assert updated.id == event.id
    assert updated.status == "eligible"
    assert updated.starts_at == candidate.starts_at
    assert updated.ends_at is None
    assert updated.price_status == "free"
    assert changes == []
    assert expire_past_events(session) == 0
    assert [listed.id for listed in list_events(session)] == [event.id]
    source = session.exec(select(EventSource).where(EventSource.source_url == weaker.source_url)).one()
    assert source.evidence["unmerged_facts"]["ends_at"] == weaker.ends_at.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_trust", ["low", "high"])
async def test_trusted_eligibility_rejection_remains_authoritative(
    session, candidate, config, organizers, initial_trust
):
    official = candidate.model_copy(deep=True)
    if initial_trust == "low":
        candidate.organizer = "Unknown organizer"
        candidate.source_name = "luma_bengaluru"
    event, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    assert event.organizer_trust == initial_trust
    official.source_url = "https://events.example.test/official-eligibility"
    official.eligibility_text = "Students only"
    assessment = await assess_candidate(official, config, organizers)
    assert assessment.organizer_trust == "high"

    updated, changes, _ = upsert_candidate(session, official, assessment)

    assert updated.id == event.id
    assert updated.status == "rejected"
    assert updated.eligibility_text == "Students only"
    assert "lifecycle" in {change.change_type for change in changes}


@pytest.mark.asyncio
async def test_weaker_terminal_cross_posting_cannot_override_trusted_event(
    session, candidate, config, organizers
):
    event, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    assert event is not None
    weaker = candidate.model_copy(deep=True)
    weaker.organizer = "Unknown organizer"
    weaker.source_name = "luma_bengaluru"
    weaker.registration_state = RegistrationState.CANCELLED
    updated, changes, _ = upsert_candidate(session, weaker, await assess_candidate(weaker, config, organizers))
    assert updated is not None
    assert updated.registration_state == "open"
    assert updated.status == "eligible"
    assert not {change.change_type for change in changes} & {"registration_state", "lifecycle"}


@pytest.mark.asyncio
async def test_unverified_organizer_alias_cannot_gain_merge_authority(
    session, candidate, config, organizers
):
    event, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    assert event is not None
    assert event.organizer_trust == "high"

    spoofed = candidate.model_copy(deep=True)
    spoofed.source_name = "luma_bengaluru"
    spoofed.source_url = "https://lu.ma/third-party-event"
    spoofed.canonical_url = "https://lu.ma/third-party-event"
    spoofed.description = "Unverified listing overwrite"
    assessment = await assess_candidate(spoofed, config, organizers)
    assert assessment.organizer_trust == "low"

    updated, changes, _ = upsert_candidate(session, spoofed, assessment)
    assert updated is not None
    assert updated.description == candidate.description
    assert not changes


@pytest.mark.asyncio
async def test_sparse_cross_posting_does_not_demote_or_erase_known_facts(
    session, candidate, config, organizers
):
    event, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    assert event is not None
    sparse = candidate.model_copy(deep=True)
    sparse.starts_at = None
    sparse.ends_at = None
    sparse.venue = None
    sparse.city = None
    sparse.format = EventFormat.UNKNOWN
    sparse.registration_state = RegistrationState.UNKNOWN
    updated, _, _ = upsert_candidate(session, sparse, await assess_candidate(sparse, config, organizers))
    assert updated is not None
    assert updated.status == "eligible"
    assert updated.starts_at == candidate.starts_at
    assert updated.city == "Bengaluru"


@pytest.mark.asyncio
async def test_same_day_sessions_remain_distinct_and_past_events_expire(session, candidate, config, organizers):
    first, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    assert first is not None
    second = candidate.model_copy(deep=True)
    second.canonical_url = "https://events.example.test/ai-systems-afternoon"
    second.starts_at = candidate.starts_at + timedelta(minutes=1)
    second.format = EventFormat.IN_PERSON
    distinct, _, created = upsert_candidate(session, second, await assess_candidate(second, config, organizers))
    assert distinct is not None
    assert created is True

    past = Event(
        canonical_url="https://events.example.test/past",
        normalized_key="past",
        title="Past AI talk",
        starts_at=datetime.now(UTC) - timedelta(hours=1),
        status="eligible",
    )
    session.add(past)
    session.commit()
    assert expire_past_events(session) == 1
    assert not [event for event in list_events(session) if event.id == past.id]


@pytest.mark.asyncio
async def test_multi_day_event_remains_visible_until_its_end(session, candidate, config, organizers):
    event, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    assert event is not None
    event.starts_at = datetime.now(UTC) - timedelta(hours=1)
    event.ends_at = datetime.now(UTC) + timedelta(hours=2)
    session.add(event)
    session.commit()
    assert event.status == "eligible"
    assert event in list_events(session)
    assert expire_past_events(session) == 0


def test_list_events_event_types_opened_after_and_bengaluru_filters_are_backward_compatible(session):
    now = datetime.now(UTC)
    bengaluru_hackathon = Event(
        canonical_url="https://events.example.test/blr-hackathon",
        normalized_key="blr-hackathon",
        title="Bengaluru Robotics Hackathon",
        event_type="hackathon",
        city="Bengaluru",
        venue="BLR Convention Centre",
        starts_at=now + timedelta(days=10),
        first_observed_open_at=now - timedelta(days=1),
        status="eligible",
    )
    remote_meetup = Event(
        canonical_url="https://events.example.test/remote-meetup",
        normalized_key="remote-meetup",
        title="Remote Systems Meetup",
        event_type="meetup",
        city="Hyderabad",
        venue="Tech Park",
        starts_at=now + timedelta(days=10),
        first_observed_open_at=now - timedelta(days=30),
        status="eligible",
    )
    case_insensitive_bengaluru = Event(
        canonical_url="https://events.example.test/bangalore-workshop",
        normalized_key="bangalore-workshop",
        title="Bangalore AI Workshop",
        event_type="workshop",
        city="BANGALORE",
        venue=None,
        starts_at=now + timedelta(days=10),
        registration_opened_at=now - timedelta(hours=1),
        status="eligible",
    )
    session.add_all([bengaluru_hackathon, remote_meetup, case_insensitive_bengaluru])
    session.commit()

    # Existing no-arg behavior is unchanged: all three eligible events return.
    assert {event.id for event in list_events(session)} == {
        bengaluru_hackathon.id,
        remote_meetup.id,
        case_insensitive_bengaluru.id,
    }

    by_type = list_events(session, event_types=["hackathon", "workshop"])
    assert {event.id for event in by_type} == {bengaluru_hackathon.id, case_insensitive_bengaluru.id}

    recently_opened = list_events(session, opened_after=now - timedelta(days=7))
    assert {event.id for event in recently_opened} == {bengaluru_hackathon.id, case_insensitive_bengaluru.id}

    bengaluru_only = list_events(session, bengaluru_only=True)
    assert {event.id for event in bengaluru_only} == {bengaluru_hackathon.id, case_insensitive_bengaluru.id}
    assert remote_meetup.id not in {event.id for event in bengaluru_only}


@pytest.mark.asyncio
async def test_schedule_merge_recomputes_normalized_dedupe_key(session, candidate, config, organizers):
    event, _, _ = upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    assert event is not None
    prior = event.normalized_key
    changed = candidate.model_copy(deep=True)
    changed.starts_at += timedelta(hours=1)
    updated, _, _ = upsert_candidate(session, changed, await assess_candidate(changed, config, organizers))
    assert updated is not None
    assert updated.normalized_key == normalized_event_key(updated)
    assert updated.normalized_key != prior
