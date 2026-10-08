"""Durable event state: sparse merge, source provenance, lifecycle, and digest changes."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import urlsplit

from sqlalchemy import or_
from sqlmodel import Session, select

from eventfinder.config import FileConfig, get_file_config
from eventfinder.domain import (
    EventCandidate,
    RegistrationState,
    SourceEvidence,
    admission_price_status,
    candidate_admission_statement,
    candidate_admission_status,
    event_facts_match,
    free_only_from_meetup_fee_settings,
    has_explicit_paid_price,
    observation_facts,
)
from eventfinder.models import (
    DigestDelivery,
    DigestRun,
    Event,
    EventChange,
    EventSource,
    SourceRun,
    utcnow,
)
from eventfinder.policy import Assessment, notification_policy_violation
from eventfinder.urls import (
    meetup_event_identity_url,
    nvidia_webinar_identity_url,
    same_event_destination,
)

# Mirrors the alias set in policy.BENGALURU_PATTERN (kept independent so this
# module can build a SQL-level, case-insensitive LIKE predicate over the
# stored city/venue columns rather than re-matching free text in Python).
BENGALURU_ALIASES = ("bengaluru", "bangalore", "blr")

MATERIAL_FIELDS = {
    "registration_state": "registration_state",
    "registration_url": "registration_url",
    "registration_opened_at": "registration_opened",
    "first_observed_open_at": "registration_opened",
    "starts_at": "schedule",
    "ends_at": "schedule",
    "venue": "venue",
    "format": "format",
    "registration_deadline": "registration_deadline",
    "price_status": "price",
    "status": "lifecycle",
}
TERMINAL_STATES = {"cancelled", "sold_out", "closed", "postponed"}
TRUST_RANK = {"low": 0, "medium": 1, "high": 2}


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def normalized_key(candidate: EventCandidate) -> str:
    title = re.sub(r"[^a-z0-9]+", "", candidate.title.casefold())
    organizer = re.sub(r"[^a-z0-9]+", "", (candidate.organizer or "").casefold())
    starts_at = _as_utc(candidate.starts_at).strftime("%Y%m%d%H%M") if candidate.starts_at else "unknown"
    venue = re.sub(r"[^a-z0-9]+", "", (candidate.venue or candidate.city or "").casefold())
    return ":".join((title, organizer, starts_at, venue, candidate.format.value))


def normalized_event_key(event: Event) -> str:
    title = re.sub(r"[^a-z0-9]+", "", event.title.casefold())
    organizer = re.sub(r"[^a-z0-9]+", "", (event.organizer or "").casefold())
    starts_at = _as_utc(event.starts_at).strftime("%Y%m%d%H%M") if event.starts_at else "unknown"
    venue = re.sub(r"[^a-z0-9]+", "", (event.venue or event.city or "").casefold())
    return ":".join((title, organizer, starts_at, venue, event.format))


def _text_value(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value).isoformat()
    return str(value)


def _price_status(candidate: EventCandidate) -> str:
    return candidate_admission_status(candidate)


def stored_candidate(event: Event) -> EventCandidate:
    fields = EventCandidate.model_fields.keys() & Event.model_fields.keys()
    return EventCandidate(**{field: getattr(event, field) for field in fields},
        source_name="saved_record", source_url=event.canonical_url,
        is_explicitly_paid=event.price_status == "paid",
        evidence=SourceEvidence(source_name="saved_record", source_url=event.canonical_url,
                                observed_at=event.last_seen_at))


def _legacy_unverified_free(session: Session, event: Event) -> bool:
    """Old semantic parsers converted any page-wide 'free' into a fee fact."""

    if admission_price_status(event.price_text) != "free":
        return False
    facts = []
    for source in session.exec(select(EventSource).where(EventSource.event_id == event.id)):
        facts.extend(observation_facts(source.evidence))
    legacy = any(fact.get("legacy_unverified_admission") is True
                 or (str(fact.get("parser", "")).endswith(":semantic_labels")
                     and "admission_price" not in fact) for fact in facts)
    candidate = stored_candidate(event)
    matching_facts = [fact for fact in facts if event_facts_match(candidate, fact)]
    verified = any(admission_price_status(fact.get("admission_price")) == "free"
                   for fact in matching_facts)
    candidate.evidence.facts["merged_observations"] = [{"facts": fact} for fact in matching_facts]
    verified = verified or candidate_admission_statement(candidate) is not None
    for fact in matching_facts:
        node = fact.get("event", {})
        if not isinstance(node, dict):
            continue
        values = [node.get(key) for key in ("price", "fee", "price_text")]
        offers = node.get("offers", [])
        offers = offers if isinstance(offers, list) else [offers]
        values.extend(offer.get("price") if isinstance(offer, dict) else offer for offer in offers)
        verified = verified or node.get("isAccessibleForFree") is True or any(
            admission_price_status(value) == "free" for value in values
        )
    return legacy and not verified


def event_policy_violation(session: Session, event: Event, config: FileConfig | None = None,
                           now: datetime | None = None, *, check_time: bool = True) -> tuple[str, str] | None:
    candidate = stored_candidate(event)
    candidate.evidence.facts["merged_observations"] = [
        {"facts": source.evidence}
        for source in session.exec(select(EventSource).where(EventSource.event_id == event.id))
    ]
    violation = notification_policy_violation(candidate, config or get_file_config(),
                                              _as_utc(now or utcnow()), check_time=check_time)
    if violation:
        return violation
    if _legacy_unverified_free(session, event):
        return "needs_review", "Legacy free admission lacks event-specific evidence"
    return None


def revalidate_notification_policy(session: Session, config: FileConfig | None = None,
                                   now: datetime | None = None) -> dict[str, int]:
    """Recheck saved facts without fetching sources, classifying, or sending."""

    counts = {"checked": 0, "needs_review": 0, "rejected": 0}
    for event in session.exec(select(Event).where(Event.status == "eligible")).all():
        counts["checked"] += 1
        violation = event_policy_violation(session, event, config, now)
        if not violation:
            if event.price_status != "free":
                event.price_status = "free"
                session.add(event)
            continue
        event.status, event.relevance_reason = violation
        event.price_status = _price_status(stored_candidate(event))
        if _legacy_unverified_free(session, event):
            event.price_status = "not_stated"
        event.updated_at = now or utcnow()
        session.add(event)
        counts[event.status] += 1
    session.commit()
    return counts


def notification_allowed(session: Session, event: Event, change: EventChange,
                         now: datetime | None = None) -> bool:
    terminal = event.registration_state in TERMINAL_STATES and change.change_type in {
        "registration_state", "lifecycle"
    }
    if terminal and not _previously_notified(session, event):
        return False
    return (event.status == "eligible" or terminal) and event_policy_violation(
        session, event, now=now, check_time=not terminal
    ) is None


def _previously_notified(session: Session, event: Event) -> bool:
    """Suppressed changes are not delivery history; require a sent message."""

    change_ids = set(session.exec(select(EventChange.id).where(EventChange.event_id == event.id)))
    if not change_ids:
        return False
    for delivery in session.exec(select(DigestDelivery).where(DigestDelivery.sent_at.is_not(None))):
        if delivery.event_change_ids is not None:
            if change_ids.intersection(delivery.event_change_ids):
                return True
            continue
        # Legacy chunks have no attribution. A wholly delivered run proves
        # its listed changes were sent; a partial run cannot prove which ones.
        run = session.get(DigestRun, delivery.digest_run_id)
        if run and change_ids.intersection(run.event_change_ids):
            chunks = session.exec(select(DigestDelivery).where(DigestDelivery.digest_run_id == run.id)).all()
            if chunks and all(chunk.sent_at is not None for chunk in chunks):
                return True
    return False


def _present(value: object, unknown: str | None = None) -> bool:
    if value is None or value == "" or value == []:
        return False
    return not (isinstance(value, str) and unknown and value == unknown)


def _candidate_trust_wins(event: Event, assessment: Assessment) -> bool:
    return TRUST_RANK.get(assessment.organizer_trust, 0) >= TRUST_RANK.get(event.organizer_trust, 0)


def _meetup_survivor_rank(event: Event) -> tuple[bool, int, int, int]:
    """Keep one eligible row, then prefer more observed facts with stable ties."""

    facts = sum((
        event.starts_at is not None,
        event.ends_at is not None,
        bool(event.venue),
        bool(event.city),
        event.format != "unknown",
        bool(event.organizer),
        bool(event.description),
        event.registration_state != "unknown",
        event.registration_deadline is not None,
    ))
    return (event.status == "eligible", facts, event.score, -(event.id or 0))


def find_existing(session: Session, candidate: EventCandidate) -> Event | None:
    meetup_identity = meetup_event_identity_url(candidate.canonical_url)
    if meetup_identity:
        parsed = urlsplit(meetup_identity)
        path_prefix = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        matches = [
            match
            for match in session.exec(select(Event).where(Event.canonical_url.startswith(path_prefix)))
            if meetup_event_identity_url(match.canonical_url) == meetup_identity
        ]
        if matches:
            return max(matches, key=_meetup_survivor_rank)
    event = session.exec(select(Event).where(Event.canonical_url == candidate.canonical_url)).first()
    if event:
        return event
    event = session.exec(select(Event).where(Event.normalized_key == normalized_key(candidate))).first()
    if event and meetup_identity:
        existing_meetup_identity = meetup_event_identity_url(event.canonical_url)
        if existing_meetup_identity and existing_meetup_identity != meetup_identity:
            return None
    if event and (nvidia_identity := nvidia_webinar_identity_url(candidate.canonical_url)):
        existing_nvidia_identity = nvidia_webinar_identity_url(event.canonical_url)
        if existing_nvidia_identity and existing_nvidia_identity != nvidia_identity:
            return None
    return event


def _merge_scalar(event: Event, field: str, value: object, assessment: Assessment, unknown: str | None = None) -> None:
    if not _present(value, unknown):
        return
    previous = getattr(event, field)
    if not _present(previous, unknown) or _candidate_trust_wins(event, assessment):
        setattr(event, field, value)


def _merge_registration_state(event: Event, candidate: EventCandidate, assessment: Assessment) -> bool:
    incoming = candidate.registration_state.value
    if incoming == RegistrationState.UNKNOWN.value:
        return True
    existing = event.registration_state
    incoming_terminal = incoming in TERMINAL_STATES
    existing_terminal = existing in TERMINAL_STATES
    # Do not let a weaker cross-posting cancel or close an event confirmed by a
    # more trusted organizer. Equal trust preserves first-observed terminal facts.
    if incoming_terminal and not _candidate_trust_wins(event, assessment):
        return False
    if existing_terminal and not incoming_terminal and not _candidate_trust_wins(event, assessment):
        return False
    if not existing_terminal or _candidate_trust_wins(event, assessment):
        event.registration_state = incoming
        return True
    return False


def _merge_candidate(event: Event, candidate: EventCandidate, assessment: Assessment,
                     config: FileConfig | None = None) -> None:
    previous_state = event.registration_state
    trusted_rejection = assessment.status != "rejected" or _candidate_trust_wins(event, assessment)
    # Meetup's empty fee setting is weak evidence; it never overrides stored paid facts.
    weak_free = (
        event.id is not None
        and (event.price_status == "paid" or has_explicit_paid_price(event.price_text))
        and free_only_from_meetup_fee_settings(candidate)
    )
    _merge_scalar(event, "title", candidate.title, assessment)
    for field, value in (
        ("organizer", candidate.organizer), ("description", candidate.description),
        ("venue", candidate.venue),
        ("city", candidate.city), ("country", candidate.country),
        ("registration_url", candidate.registration_url),
        ("registration_deadline", candidate.registration_deadline),
        ("registration_opened_at", candidate.registration_opened_at),
        ("first_observed_open_at", candidate.first_observed_open_at),
    ):
        _merge_scalar(event, field, value, assessment)
    # Rejected weaker evidence cannot fill policy-sensitive gaps in an
    # accepted event while its lifecycle rejection is deliberately ignored.
    if trusted_rejection:
        _merge_scalar(event, "starts_at", candidate.starts_at, assessment)
        _merge_scalar(event, "ends_at", candidate.ends_at, assessment)
        if not weak_free:
            _merge_scalar(event, "price_text", candidate.price_text, assessment)
        _merge_scalar(event, "eligibility_text", candidate.eligibility_text, assessment)
    _merge_scalar(event, "format", candidate.format.value, assessment, RegistrationState.UNKNOWN.value)
    _merge_scalar(event, "event_type", candidate.event_type.value, assessment, "unknown")
    registration_state_accepted = _merge_registration_state(event, candidate, assessment)
    if (
        event.id is not None
        and previous_state in {"unknown", "closed"}
        and event.registration_state == "open"
        and event.first_observed_open_at is None
    ):
        event.first_observed_open_at = candidate.first_observed_open_at or candidate.evidence.observed_at
    candidate_price = _price_status(candidate)
    if trusted_rejection and not weak_free and candidate_price != "not_stated" and (event.price_status == "not_stated" or _candidate_trust_wins(event, assessment)):
        event.price_status = candidate_price
    event.speakers = sorted(set(event.speakers) | set(candidate.speakers))
    event.topics = sorted(set(event.topics) | set(candidate.topics))
    if assessment.concise_summary:
        event.concise_summary = assessment.concise_summary
    if assessment.ai_provenance:
        event.ai_provenance = assessment.ai_provenance
    event.organizer_trust = max((event.organizer_trust, assessment.organizer_trust), key=lambda x: TRUST_RANK.get(x, 0))
    event.approval_required = event.approval_required or assessment.approval_required
    if trusted_rejection:
        event.relevance_reason = assessment.reason
    event.score = max(event.score, assessment.score)
    state_conflict = (
        candidate.registration_state.value in TERMINAL_STATES
        or event.registration_state in TERMINAL_STATES
    ) and not registration_state_accepted
    visibility_end = event.ends_at or event.starts_at
    if visibility_end and _as_utc(visibility_end) < utcnow():
        event.status = "expired"
    elif not state_conflict and trusted_rejection:
        # A sparse re-observation should not demote an already-qualified event
        # merely because that cross-posting omitted facts we still retain.
        if event.id is None or not (
            event.status == "eligible" and assessment.status == "needs_review"
        ):
            event.status = assessment.status
    event.last_seen_at = utcnow()
    event.updated_at = utcnow()
    event.normalized_key = normalized_event_key(event)
    # Evaluate retained facts after a sparse merge. Missing fields still cannot
    # erase valid evidence, but an old eligible row cannot bypass new gates.
    retained = stored_candidate(event)
    if trusted_rejection:
        retained.evidence = candidate.evidence
    violation = notification_policy_violation(retained, config or get_file_config(), utcnow())
    if event.status == "eligible" and violation:
        event.status, event.relevance_reason = violation


def _new_event(candidate: EventCandidate, assessment: Assessment, config: FileConfig | None = None) -> Event:
    event = Event(canonical_url=candidate.canonical_url, normalized_key=normalized_key(candidate), title=candidate.title)
    event.registration_state = "unknown"
    event.price_status = "not_stated"
    event.organizer_trust = "low"
    _merge_candidate(event, candidate, assessment, config)
    event.first_seen_at = utcnow()
    return event


def _refresh_provenance(session: Session, event: Event, candidate: EventCandidate, assessment: Assessment) -> None:
    assert event.id is not None
    source = session.exec(select(EventSource).where(EventSource.event_id == event.id, EventSource.source_url == candidate.source_url)).first()
    evidence = {**candidate.evidence.facts, "organizer_trust": assessment.organizer_trust}
    if source and any(
        fact.get("legacy_unverified_admission") is True
        or (str(fact.get("parser", "")).endswith(":semantic_labels") and "admission_price" not in fact)
        for fact in observation_facts(source.evidence)
    ):
        evidence["legacy_unverified_admission"] = True
    if source and source.evidence.get("admission_review_refresh_at"):
        evidence["admission_review_refresh_at"] = source.evidence["admission_review_refresh_at"]
    if _candidate_trust_wins(event, assessment):
        statement = candidate_admission_statement(candidate)
        if statement:
            evidence["admission_statement"] = statement
        elif source and source.evidence.get("admission_statement"):
            evidence["admission_statement"] = source.evidence["admission_statement"]
    if candidate.price_text and _candidate_trust_wins(event, assessment):
        evidence["admission_price"] = candidate.price_text
    elif source and source.evidence.get("admission_price"):
        evidence["admission_price"] = source.evidence["admission_price"]
    if assessment.status == "rejected" and not _candidate_trust_wins(event, assessment):
        unmerged = {
            field: value
            for field, value in (
                ("starts_at", _text_value(candidate.starts_at)),
                ("ends_at", _text_value(candidate.ends_at)),
                ("price_text", candidate.price_text),
                ("is_explicitly_paid", True if candidate.is_explicitly_paid else None),
                ("eligibility_text", candidate.eligibility_text),
            )
            if _present(value)
        }
        if unmerged:
            evidence["unmerged_facts"] = unmerged
    if source is None:
        session.add(EventSource(event_id=event.id, source_name=candidate.source_name, source_url=candidate.source_url, raw_id=candidate.evidence.raw_id, evidence=evidence, observed_at=candidate.evidence.observed_at))
    else:
        source.source_name, source.raw_id, source.evidence, source.observed_at = candidate.source_name, candidate.evidence.raw_id, evidence, candidate.evidence.observed_at
        session.add(source)
    event.source_urls = sorted(set([*event.source_urls, candidate.source_url]))


def upsert_candidate(session: Session, candidate: EventCandidate, assessment: Assessment,
                     config: FileConfig | None = None) -> tuple[Event | None, list[EventChange], bool]:
    """Persist accepted/reviewed candidates and only existing rejected candidates."""
    event = find_existing(session, candidate)
    if event is None and assessment.status == "rejected":
        return None, [], False
    created = event is None
    if event is None:
        event = _new_event(candidate, assessment, config)
        session.add(event)
        session.flush()
        assert event.id is not None
        changes = [EventChange(event_id=event.id, change_type="new_event", new_value="qualified")]
    else:
        old_values = {field: getattr(event, field) for field in MATERIAL_FIELDS}
        _merge_candidate(event, candidate, assessment, config)
        session.add(event)
        session.flush()
        assert event.id is not None
        changes = []
        for field, kind in MATERIAL_FIELDS.items():
            old_value = _text_value(old_values[field])
            new_value = _text_value(getattr(event, field))
            if old_value == new_value or (
                field == "registration_url" and same_event_destination(old_value, new_value)
            ):
                continue
            changes.append(EventChange(
                event_id=event.id, change_type=kind, old_value=old_value, new_value=new_value
            ))
    _refresh_provenance(session, event, candidate, assessment)
    session.add_all(changes)
    session.commit()
    session.refresh(event)
    return event, changes, created


def expire_past_events(session: Session, now: datetime | None = None) -> int:
    now = now or utcnow()
    expired = 0
    for event in session.exec(select(Event).where(Event.status == "eligible")).all():
        visibility_end = event.ends_at or event.starts_at
        if visibility_end and _as_utc(visibility_end) < now:
            event.status = "expired"
            event.updated_at = now
            session.add(event)
            session.add(EventChange(event_id=event.id, change_type="expired", old_value="eligible", new_value="expired"))
            expired += 1
    if expired:
        session.commit()
    return expired


def start_source_run(session: Session, source_name: str) -> SourceRun:
    run = SourceRun(source_name=source_name)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def finish_source_run(session: Session, run: SourceRun, *, fetched_count: int = 0, accepted_count: int = 0, rejected_count: int = 0, error: str | None = None, status_code: int | None = None) -> None:
    run.finished_at, run.fetched_count, run.accepted_count, run.rejected_count = utcnow(), fetched_count, accepted_count, rejected_count
    run.error, run.status_code = error, status_code
    session.add(run)
    session.commit()


def _phrase_match(needle: str, haystack: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(needle.casefold())}(?!\w)", haystack.casefold()))


def list_events(session: Session, *, text: str | None = None, topic: str | None = None, event_type: str | None = None, event_types: list[str] | None = None, organizer: str | None = None, event_format: str | None = None, registration_state: str | None = None, source: str | None = None, start_after: datetime | None = None, start_before: datetime | None = None, start_before_exclusive: bool = False, opened_after: datetime | None = None, bengaluru_only: bool = False, status: str | None = None, limit: int = 100) -> list[Event]:
    statement = select(Event).where(Event.status == (status or "eligible"))
    if text:
        statement = statement.where((Event.title.ilike(f"%{text}%")) | (Event.description.ilike(f"%{text}%")))
    if event_type:
        statement = statement.where(Event.event_type == event_type)
    if event_types:
        statement = statement.where(Event.event_type.in_(event_types))
    if organizer:
        statement = statement.where(Event.organizer.ilike(f"%{organizer}%"))
    if event_format:
        statement = statement.where(Event.format == event_format)
    if registration_state:
        statement = statement.where(Event.registration_state == registration_state)
    if start_after:
        statement = statement.where(Event.starts_at >= _as_utc(start_after))
    if start_before:
        bound = _as_utc(start_before)
        statement = statement.where(Event.starts_at < bound if start_before_exclusive else Event.starts_at <= bound)
    if opened_after:
        # Either column can carry the "registration opened" fact depending on
        # whether the open transition was explicitly dated or only observed.
        statement = statement.where(
            or_(Event.registration_opened_at >= opened_after, Event.first_observed_open_at >= opened_after)
        )
    if bengaluru_only:
        statement = statement.where(
            or_(*(or_(Event.city.ilike(f"%{alias}%"), Event.venue.ilike(f"%{alias}%")) for alias in BENGALURU_ALIASES))
        )
    events = [
        event
        for event in session.exec(statement).all()
        if not (event.ends_at or event.starts_at) or _as_utc(event.ends_at or event.starts_at) >= utcnow()
    ]
    if topic:
        events = [event for event in events if _phrase_match(topic, " ".join([event.title, event.description or "", *event.topics]))]
    if source:
        ids = set(session.exec(select(EventSource.event_id).where(EventSource.source_name.ilike(f"%{source}%"))).all())
        events = [event for event in events if event.id in ids]
    return sorted(events, key=lambda event: (-event.score, _as_utc(event.starts_at) if event.starts_at else datetime.max.replace(tzinfo=UTC)))[:limit]


def source_health(session: Session) -> list[dict[str, object]]:
    latest: dict[str, SourceRun] = {}
    for run in session.exec(select(SourceRun).order_by(SourceRun.started_at.desc())).all():
        latest.setdefault(run.source_name, run)
    return [{"source_name": name, "last_started_at": run.started_at, "last_finished_at": run.finished_at, "status": "error" if run.error else "degraded" if run.fetched_count == 0 else "ok", "error": run.error or ("zero candidates" if run.fetched_count == 0 else None), "fetched_count": run.fetched_count, "accepted_count": run.accepted_count} for name, run in sorted(latest.items())]


def pending_changes(session: Session, now: datetime | None = None) -> list[tuple[EventChange, Event]]:
    result: list[tuple[EventChange, Event]] = []
    for change in session.exec(select(EventChange).where(EventChange.digested_at.is_(None)).order_by(EventChange.observed_at)).all():
        event = session.get(Event, change.event_id)
        terminal_transition = (
            event
            and event.registration_state in TERMINAL_STATES
            and change.change_type in {"registration_state", "lifecycle"}
        )
        visible_eligible = event and event.status == "eligible" and (
            not (event.ends_at or event.starts_at)
            or _as_utc(event.ends_at or event.starts_at) >= _as_utc(now or utcnow())
        )
        if (visible_eligible or terminal_transition) and notification_allowed(session, event, change, now):
            result.append((change, event))
    return result


def mark_changes_digested(session: Session, changes: Iterable[EventChange]) -> None:
    for change in changes:
        change.digested_at = utcnow()
        session.add(change)
    session.commit()
