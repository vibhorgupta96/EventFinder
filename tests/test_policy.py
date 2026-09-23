from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from eventfinder.ai import AIUnavailable
from eventfinder.domain import EventFormat, EventType
from eventfinder.policy import assess_candidate, match_organizer


@pytest.mark.asyncio
async def test_bengaluru_technical_event_is_eligible(candidate, config, organizers):
    assessment = await assess_candidate(candidate, config, organizers)
    assert assessment.status == "eligible"
    assert assessment.score >= config.ranking.minimum_score


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("description", "paid", "expected"),
    [
        ("Founder startup pitch night", False, "rejected"),
        ("Technical AI event for students only", False, "rejected"),
        ("Technical AI event", True, "rejected"),
    ],
)
async def test_explicit_policy_exclusions(candidate, config, organizers, description, paid, expected):
    candidate.description = description
    candidate.is_explicitly_paid = paid
    assert (await assess_candidate(candidate, config, organizers)).status == expected


@pytest.mark.asyncio
async def test_unknown_price_is_eligible_and_approval_is_badged(candidate, config, organizers):
    candidate.price_text = None
    candidate.eligibility_text = "Application review; approval required before entry"
    assessment = await assess_candidate(candidate, config, organizers)
    assert assessment.status == "eligible"
    assert assessment.approval_required is True


@pytest.mark.asyncio
async def test_trusted_global_online_is_allowed(candidate, config, organizers):
    candidate.city = "New York"
    candidate.venue = None
    candidate.format = EventFormat.ONLINE
    candidate.organizer = "Microsoft Reactor"
    assert (await assess_candidate(candidate, config, organizers)).status == "eligible"


@pytest.mark.asyncio
async def test_untrusted_global_online_is_rejected(candidate, config, organizers):
    candidate.city = "New York"
    candidate.venue = None
    candidate.format = EventFormat.ONLINE
    candidate.organizer = "Unknown Organizer"
    assert (await assess_candidate(candidate, config, organizers)).status == "rejected"


def test_multi_tenant_listing_domains_do_not_confer_organizer_trust(candidate, organizers):
    candidate.canonical_url = "https://lu.ma/third-party-event"
    candidate.organizer = "Unknown Organizer"
    candidate.source_name = "luma_bengaluru"
    assert match_organizer(candidate, organizers).trust == "low"
    candidate.canonical_url = "https://www.meetup.com/unknown/events/1"
    assert match_organizer(candidate, organizers).trust == "low"


@pytest.mark.asyncio
async def test_extended_window_requires_recent_registration_opening(candidate, config, organizers):
    candidate.starts_at = datetime.now(UTC) + timedelta(days=100)
    assert (await assess_candidate(candidate, config, organizers)).status == "needs_review"
    candidate.registration_opened_at = datetime.now(UTC) - timedelta(days=2)
    assert (await assess_candidate(candidate, config, organizers)).status == "eligible"


@pytest.mark.asyncio
async def test_ongoing_multi_day_event_is_eligible_after_its_start(candidate, config, organizers):
    now = datetime.now(UTC)
    candidate.starts_at = now - timedelta(days=2)
    candidate.ends_at = now + timedelta(days=1)
    assert (await assess_candidate(candidate, config, organizers, now=now)).status == "eligible"


@pytest.mark.asyncio
async def test_deadline_score_requires_a_future_registration_deadline(candidate, config, organizers):
    now = datetime.now(UTC)
    baseline = await assess_candidate(candidate, config, organizers, now=now)
    candidate.registration_deadline = now + timedelta(days=2)
    timely = await assess_candidate(candidate, config, organizers, now=now)
    assert timely.status == "eligible"
    assert timely.score == baseline.score + 2
    candidate.registration_deadline = now - timedelta(minutes=1)
    expired = await assess_candidate(candidate, config, organizers, now=now)
    assert expired.status == "needs_review"
    assert expired.score == 0


@pytest.mark.asyncio
async def test_ai_outage_leaves_ambiguous_candidate_for_review(candidate, config, organizers):
    candidate.title = "Practitioner Session"
    candidate.description = "A session."
    candidate.event_type = EventType.UNKNOWN

    class Unavailable:
        async def classify(self, _candidate):
            raise AIUnavailable("offline")

    assessment = await assess_candidate(candidate, config, organizers, classifier=Unavailable())
    assert assessment.status == "needs_review"
