from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from eventfinder.domain import EventFormat, RegistrationState
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
