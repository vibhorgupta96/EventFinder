"""Explicit eligibility policy and deterministic event ranking."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from eventfinder.ai import AIClassifier, AIUnavailable
from eventfinder.config import FileConfig, OrganizersRegistry
from eventfinder.domain import (
    EventCandidate,
    EventFormat,
    EventType,
    RegistrationState,
    has_explicit_paid_price,
)

EXCLUDED_TERMS = (
    "product management",
    "product meetup",
    "founder",
    "startup pitch",
    "pitch night",
    "sales",
    "career fair",
    "job fair",
    "networking mixer",
    "networking event",
)
INELIGIBLE_TERMS = (
    "students only",
    "student-only",
    "employees only",
    "employee-only",
    "invite only",
    "private event",
)
APPROVAL_TERMS = ("approval required", "application review", "subject to approval", "apply to attend")
BENGALURU_PATTERN = re.compile(r"\b(?:bengaluru|bangalore|blr)\b", re.IGNORECASE)


@dataclass(frozen=True)
class OrganizerMatch:
    name: str | None
    trust: str
    online_allowed: bool
    explicit_identity: bool


@dataclass(frozen=True)
class Assessment:
    status: str  # eligible, needs_review, rejected
    reason: str
    score: int
    approval_required: bool
    organizer_trust: str
    concise_summary: str | None = None
    ai_provenance: dict[str, str] | None = None


def match_organizer(candidate: EventCandidate, registry: OrganizersRegistry) -> OrganizerMatch:
    organizer_text = (candidate.organizer or "").casefold()
    hostname = (urlparse(candidate.canonical_url).hostname or "").casefold()
    alias_match: OrganizerMatch | None = None
    profile_match: OrganizerMatch | None = None
    for organizer in registry.organizers:
        aliases = [organizer.name, *organizer.aliases]
        matches_alias = any(
            re.search(rf"(?<!\w){re.escape(alias.casefold())}(?!\w)", organizer_text)
            for alias in aliases
        )
        # A configured source profile provides provenance, but an online event
        # still needs an organizer identity from its own public facts. A generic
        # listing source is not evidence that every event it hosts is trusted.
        matches_profile = candidate.source_name in organizer.source_profiles
        # An explicit organizer label is more specific than a shared vendor
        # domain (for example, Google developer properties host multiple
        # official programs).
        owned_domain_match = any(
            hostname == domain.casefold() or hostname.endswith(f".{domain.casefold()}")
            for domain in organizer.domains
        )
        if matches_alias and (owned_domain_match or matches_profile):
            return OrganizerMatch(organizer.name, organizer.trust, organizer.online_allowed, True)
        if matches_alias and alias_match is None:
            # A name may be useful display provenance, but on a multi-tenant
            # page it cannot carry the organizer's configured trust or merge
            # authority until a source profile and alias, or an owned domain,
            # verifies that identity.
            alias_match = OrganizerMatch(
                organizer.name,
                "low",
                False,
                False,
            )
        if matches_profile and profile_match is None:
            profile_match = OrganizerMatch(
                organizer.name,
                "low",
                False,
                False,
            )
    for organizer in registry.organizers:
        owned_domain_match = any(
            hostname == domain.casefold() or hostname.endswith(f".{domain.casefold()}")
            for domain in organizer.domains
        )
        if owned_domain_match:
            return OrganizerMatch(organizer.name, organizer.trust, organizer.online_allowed, True)
    return alias_match or profile_match or OrganizerMatch(None, "low", False, False)


def is_bengaluru(candidate: EventCandidate) -> bool:
    return bool(BENGALURU_PATTERN.search(" ".join(filter(None, [candidate.city, candidate.venue]))))


def _topic_match(candidate: EventCandidate, config: FileConfig) -> bool:
    haystack = " ".join(
        filter(None, [candidate.title, candidate.description or "", *candidate.topics])
    ).lower()
    return any(
        re.search(rf"(?<!\w){re.escape(term.casefold())}(?!\w)", haystack)
        for term in config.topics.get("include", [])
    )


def _excluded(candidate: EventCandidate, config: FileConfig) -> bool:
    text = " ".join(filter(None, [candidate.title, candidate.description or ""])).lower()
    terms = set(EXCLUDED_TERMS) | {term.lower() for term in config.topics.get("exclude", [])}
    return any(term in text for term in terms)


def _ineligible(candidate: EventCandidate) -> bool:
    text = " ".join(filter(None, [candidate.eligibility_text, candidate.description])).lower()
    return any(term in text for term in INELIGIBLE_TERMS)


def _approval_required(candidate: EventCandidate) -> bool:
    text = " ".join(filter(None, [candidate.eligibility_text, candidate.description])).lower()
    return any(term in text for term in APPROVAL_TERMS)


def _within_window(candidate: EventCandidate, config: FileConfig, now: datetime) -> bool:
    if not candidate.starts_at:
        return False
    starts_at = candidate.starts_at.astimezone(UTC)
    visibility_end = (candidate.ends_at or candidate.starts_at).astimezone(UTC)
    if visibility_end < now - timedelta(days=1):
        return False
    if starts_at <= now + timedelta(days=config.policy.near_future_days):
        return True
    observed_open = candidate.registration_opened_at or candidate.first_observed_open_at
    recently_opened = observed_open and observed_open >= now - timedelta(
        days=config.policy.registration_opened_days
    )
    return bool(
        recently_opened
        and starts_at <= now + timedelta(days=config.policy.newly_opened_extended_days)
    )


def _score(candidate: EventCandidate, organizer: OrganizerMatch, now: datetime, topic_match: bool) -> int:
    score = 0
    if topic_match:
        score += 3
    if candidate.event_type != EventType.UNKNOWN:
        score += 2
    if organizer.trust == "high":
        score += 2
    elif organizer.trust == "medium":
        score += 1
    if is_bengaluru(candidate):
        score += 2
    elif candidate.format == EventFormat.ONLINE:
        score += 1
    if candidate.registration_state == RegistrationState.OPEN:
        score += 2
    elif candidate.registration_state == RegistrationState.WAITLIST:
        score += 1
    if candidate.speakers:
        score += 1
    if candidate.registration_deadline and now <= candidate.registration_deadline <= now + timedelta(days=7):
        score += 2
    observed_open = candidate.registration_opened_at or candidate.first_observed_open_at
    if observed_open and observed_open >= now - timedelta(days=7):
        score += 1
    return score


async def assess_candidate(
    candidate: EventCandidate,
    config: FileConfig,
    organizers: OrganizersRegistry,
    classifier: AIClassifier | None = None,
    now: datetime | None = None,
) -> Assessment:
    """Assess an event without ever letting AI create factual fields."""

    now = now or datetime.now(UTC)
    organizer = match_organizer(candidate, organizers)
    if candidate.is_explicitly_paid or has_explicit_paid_price(candidate.price_text):
        return Assessment("rejected", "Explicitly paid admission", 0, False, organizer.trust)
    if _excluded(candidate, config):
        return Assessment("rejected", "Excluded event category", 0, False, organizer.trust)
    if _ineligible(candidate):
        return Assessment("rejected", "Explicitly incompatible eligibility", 0, False, organizer.trust)
    if candidate.registration_state in {
        RegistrationState.CANCELLED,
        RegistrationState.POSTPONED,
        RegistrationState.SOLD_OUT,
        RegistrationState.CLOSED,
    }:
        return Assessment("rejected", f"Registration is {candidate.registration_state.value}", 0, False, organizer.trust)
    if candidate.registration_deadline and candidate.registration_deadline < now:
        return Assessment("needs_review", "Registration deadline has passed", 0, False, organizer.trust)
    if not _within_window(candidate, config, now):
        return Assessment("needs_review", "Missing or out-of-window start time", 0, False, organizer.trust)
    location_ok = is_bengaluru(candidate)
    online_ok = (
        candidate.format == EventFormat.ONLINE
        and organizer.online_allowed
        and organizer.explicit_identity
    )
    hybrid_ok = candidate.format == EventFormat.HYBRID and (
        location_ok or (organizer.online_allowed and organizer.explicit_identity)
    )
    if not (location_ok or online_ok or hybrid_ok):
        return Assessment("rejected", "Not Bengaluru or trusted global online", 0, False, organizer.trust)

    topic_match = _topic_match(candidate, config)
    ambiguous = not topic_match or candidate.event_type == EventType.UNKNOWN
    summary: str | None = None
    ai_provenance: dict[str, str] | None = None
    if ambiguous:
        if classifier is None:
            return Assessment(
                "needs_review",
                "Ambiguous technical fit",
                0,
                _approval_required(candidate),
                organizer.trust,
            )
        try:
            classification = await classifier.classify(candidate)
        except AIUnavailable:
            return Assessment(
                "needs_review",
                "Ambiguous candidate; AI classification unavailable",
                0,
                _approval_required(candidate),
                organizer.trust,
            )
        if not classification.is_technical or not classification.compatible_eligibility:
            return Assessment("rejected", "AI classification rejected ambiguous candidate", 0, False, organizer.trust)
        if candidate.event_type == EventType.UNKNOWN:
            candidate.event_type = classification.event_type
        candidate.topics = sorted(set(candidate.topics + classification.topics))
        topic_match = True
        summary = classification.concise_summary
        ai_provenance = {
            key: value
            for key, value in {
                "provider": classification.provider,
                "model": classification.model,
                "rationale": classification.rationale,
            }.items()
            if value
        }

    score = _score(candidate, organizer, now, topic_match)
    if score < config.ranking.minimum_score:
        return Assessment("needs_review", "Below deterministic ranking threshold", score, _approval_required(candidate), organizer.trust, summary, ai_provenance)
    return Assessment("eligible", "Technical event matching location and time policy", score, _approval_required(candidate), organizer.trust, summary, ai_provenance)
