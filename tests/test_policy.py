from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from eventfinder.ai import AIUnavailable
from eventfinder.domain import EventFormat, EventType
from eventfinder.policy import _within_window, assess_candidate, match_organizer


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
async def test_mixed_free_and_paid_wording_is_rejected(candidate, config, organizers):
    candidate.is_explicitly_paid = False
    candidate.price_text = "Free expo entry; paid workshop pass available"
    assessment = await assess_candidate(candidate, config, organizers)
    assert assessment.status == "rejected"
    assert assessment.reason == "Explicitly paid admission"


@pytest.mark.asyncio
@pytest.mark.parametrize(("title", "expected"), [("Salesforce AI Engineering Workshop", "eligible"), ("AI sales workshop", "rejected")])
async def test_excluded_terms_match_words_without_rejecting_salesforce(candidate, config, organizers, title, expected):
    candidate.title = title
    assert (await assess_candidate(candidate, config, organizers)).status == expected


@pytest.mark.asyncio
async def test_trusted_global_online_is_allowed(candidate, config, organizers):
    candidate.city = "New York"
    candidate.venue = None
    candidate.format = EventFormat.ONLINE
    candidate.organizer = "Microsoft Reactor"
    candidate.canonical_url = "https://developer.microsoft.com/en-us/reactor/events/example"
    assert (await assess_candidate(candidate, config, organizers)).status == "eligible"


@pytest.mark.asyncio
async def test_untrusted_global_online_is_rejected(candidate, config, organizers):
    candidate.city = "New York"
    candidate.venue = None
    candidate.format = EventFormat.ONLINE
    candidate.organizer = "Unknown Organizer"
    assert (await assess_candidate(candidate, config, organizers)).status == "rejected"


@pytest.mark.asyncio
async def test_source_profile_alone_cannot_make_transport_hosted_online_event_trusted(
    candidate, config, organizers
):
    candidate.city = "New York"
    candidate.venue = None
    candidate.format = EventFormat.ONLINE
    candidate.source_name = "cncf_bengaluru"
    candidate.canonical_url = "https://ocgroups.dev/events/details/example"
    candidate.organizer = None
    assert (await assess_candidate(candidate, config, organizers)).status == "rejected"
    candidate.organizer = "CNCF"
    assert (await assess_candidate(candidate, config, organizers)).status == "eligible"


def test_multi_tenant_listing_domains_do_not_confer_organizer_trust(candidate, organizers):
    candidate.canonical_url = "https://lu.ma/third-party-event"
    candidate.organizer = "Unknown Organizer"
    candidate.source_name = "luma_bengaluru"
    assert match_organizer(candidate, organizers).trust == "low"
    candidate.canonical_url = "https://www.meetup.com/unknown/events/1"
    assert match_organizer(candidate, organizers).trust == "low"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("canonical_url", "source_name"),
    [
        ("https://lu.ma/third-party-event", "luma_bengaluru"),
        ("https://www.meetup.com/unknown/events/1", "meetup_bengaluru"),
    ],
)
async def test_alias_on_multi_tenant_event_page_does_not_verify_online_identity(
    candidate, config, organizers, canonical_url, source_name
):
    candidate.city = "New York"
    candidate.venue = None
    candidate.format = EventFormat.ONLINE
    candidate.canonical_url = canonical_url
    candidate.source_name = source_name
    candidate.organizer = "CNCF"
    matched = match_organizer(candidate, organizers)
    assert matched.name == "Cloud Native Computing Foundation"
    assert matched.trust == "low"
    assert matched.explicit_identity is False
    assert (await assess_candidate(candidate, config, organizers)).status == "rejected"


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


def test_within_window_near_future_boundary_is_inclusive(candidate, config):
    now = datetime.now(UTC)
    candidate.ends_at = None

    candidate.starts_at = now + timedelta(days=config.policy.near_future_days)
    assert _within_window(candidate, config, now) is True

    candidate.starts_at = now + timedelta(days=config.policy.near_future_days) + timedelta(seconds=1)
    assert _within_window(candidate, config, now) is False


@pytest.mark.asyncio
async def test_assess_candidate_near_future_boundary_is_inclusive(candidate, config, organizers):
    now = datetime.now(UTC)
    candidate.ends_at = None

    candidate.starts_at = now + timedelta(days=config.policy.near_future_days)
    assert (await assess_candidate(candidate, config, organizers, now=now)).status == "eligible"

    candidate.starts_at = now + timedelta(days=config.policy.near_future_days) + timedelta(seconds=1)
    assessment = await assess_candidate(candidate, config, organizers, now=now)
    assert assessment.status == "needs_review"
    assert assessment.reason == "Missing or out-of-window start time"


def test_within_window_visibility_end_boundary(candidate, config):
    now = datetime.now(UTC)
    # An ongoing multi-day event: it started in the past, so the near-future
    # check on starts_at trivially passes and only the visibility_end cutoff
    # (ends_at < now - 1 day) is exercised.
    candidate.starts_at = now - timedelta(days=10)

    candidate.ends_at = now - timedelta(days=1)
    assert _within_window(candidate, config, now) is True

    candidate.ends_at = now - timedelta(days=1) - timedelta(seconds=1)
    assert _within_window(candidate, config, now) is False


def test_within_window_newly_opened_extension_boundary(candidate, config):
    now = datetime.now(UTC)
    candidate.ends_at = None
    # Beyond near_future_days so only the newly-opened extension can admit it.
    candidate.starts_at = now + timedelta(days=config.policy.near_future_days + 100)

    candidate.registration_opened_at = now - timedelta(days=config.policy.registration_opened_days)
    assert _within_window(candidate, config, now) is True

    candidate.registration_opened_at = now - timedelta(days=config.policy.registration_opened_days + 1)
    assert _within_window(candidate, config, now) is False


def test_within_window_newly_opened_extension_start_boundary(candidate, config):
    now = datetime.now(UTC)
    candidate.ends_at = None
    candidate.registration_opened_at = now - timedelta(days=1)

    candidate.starts_at = now + timedelta(days=config.policy.newly_opened_extended_days)
    assert _within_window(candidate, config, now) is True

    candidate.starts_at = now + timedelta(days=config.policy.newly_opened_extended_days) + timedelta(days=1)
    assert _within_window(candidate, config, now) is False


@pytest.mark.asyncio
async def test_assess_candidate_newly_opened_extension_boundary(candidate, config, organizers):
    now = datetime.now(UTC)
    candidate.ends_at = None
    candidate.starts_at = now + timedelta(days=config.policy.near_future_days + 100)

    candidate.registration_opened_at = now - timedelta(days=config.policy.registration_opened_days)
    assert (await assess_candidate(candidate, config, organizers, now=now)).status == "eligible"

    candidate.registration_opened_at = now - timedelta(days=config.policy.registration_opened_days + 1)
    assessment = await assess_candidate(candidate, config, organizers, now=now)
    assert assessment.status == "needs_review"
    assert assessment.reason == "Missing or out-of-window start time"
