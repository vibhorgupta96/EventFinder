"""Durable event state: sparse merge, source provenance, lifecycle, and digest changes."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import or_
from sqlmodel import Session, select

from eventfinder.domain import EventCandidate, RegistrationState, has_explicit_paid_price
from eventfinder.models import Event, EventChange, EventSource, SourceRun, utcnow
from eventfinder.policy import Assessment

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
    price = (candidate.price_text or "").casefold().strip()
    if candidate.is_explicitly_paid or has_explicit_paid_price(candidate.price_text):
        return "paid"
    if price in {"0", "inr 0", "usd 0", "₹0", "$0"} or any(x in price for x in ("free", "no cost")):
        return "free"
    return "not_stated"


def _present(value: object, unknown: str | None = None) -> bool:
    if value is None or value == "" or value == []:
        return False
    return not (isinstance(value, str) and unknown and value == unknown)


def _candidate_trust_wins(event: Event, assessment: Assessment) -> bool:
    return TRUST_RANK.get(assessment.organizer_trust, 0) >= TRUST_RANK.get(event.organizer_trust, 0)


def find_existing(session: Session, candidate: EventCandidate) -> Event | None:
    event = session.exec(select(Event).where(Event.canonical_url == candidate.canonical_url)).first()
    return event or session.exec(select(Event).where(Event.normalized_key == normalized_key(candidate))).first()


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


def _merge_candidate(event: Event, candidate: EventCandidate, assessment: Assessment) -> None:
    previous_state = event.registration_state
    _merge_scalar(event, "title", candidate.title, assessment)
    for field, value in (
        ("organizer", candidate.organizer), ("description", candidate.description),
        ("starts_at", candidate.starts_at), ("ends_at", candidate.ends_at), ("venue", candidate.venue),
        ("city", candidate.city), ("country", candidate.country),
        ("registration_url", candidate.registration_url),
        ("registration_deadline", candidate.registration_deadline),
        ("registration_opened_at", candidate.registration_opened_at),
        ("first_observed_open_at", candidate.first_observed_open_at),
        ("price_text", candidate.price_text), ("eligibility_text", candidate.eligibility_text),
    ):
        _merge_scalar(event, field, value, assessment)
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
    if candidate_price != "not_stated" and (event.price_status == "not_stated" or _candidate_trust_wins(event, assessment)):
        event.price_status = candidate_price
    event.speakers = sorted(set(event.speakers) | set(candidate.speakers))
    event.topics = sorted(set(event.topics) | set(candidate.topics))
    if assessment.concise_summary:
        event.concise_summary = assessment.concise_summary
    if assessment.ai_provenance:
        event.ai_provenance = assessment.ai_provenance
    event.organizer_trust = max((event.organizer_trust, assessment.organizer_trust), key=lambda x: TRUST_RANK.get(x, 0))
    event.approval_required = event.approval_required or assessment.approval_required
    event.relevance_reason = assessment.reason
    event.score = max(event.score, assessment.score)
    state_conflict = (
        candidate.registration_state.value in TERMINAL_STATES
        or event.registration_state in TERMINAL_STATES
    ) and not registration_state_accepted
    visibility_end = event.ends_at or event.starts_at
    if visibility_end and _as_utc(visibility_end) < utcnow():
        event.status = "expired"
    elif not state_conflict:
        # A sparse re-observation should not demote an already-qualified event
        # merely because that cross-posting omitted facts we still retain.
        if event.id is None or not (
            event.status == "eligible" and assessment.status == "needs_review"
        ):
            event.status = assessment.status
    event.last_seen_at = utcnow()
    event.updated_at = utcnow()
    event.normalized_key = normalized_event_key(event)


def _new_event(candidate: EventCandidate, assessment: Assessment) -> Event:
    event = Event(canonical_url=candidate.canonical_url, normalized_key=normalized_key(candidate), title=candidate.title)
    event.registration_state = "unknown"
    event.price_status = "not_stated"
    event.organizer_trust = "low"
    _merge_candidate(event, candidate, assessment)
    event.first_seen_at = utcnow()
    return event


def _refresh_provenance(session: Session, event: Event, candidate: EventCandidate, assessment: Assessment) -> None:
    assert event.id is not None
    source = session.exec(select(EventSource).where(EventSource.event_id == event.id, EventSource.source_url == candidate.source_url)).first()
    evidence = {**candidate.evidence.facts, "organizer_trust": assessment.organizer_trust}
    if source is None:
        session.add(EventSource(event_id=event.id, source_name=candidate.source_name, source_url=candidate.source_url, raw_id=candidate.evidence.raw_id, evidence=evidence, observed_at=candidate.evidence.observed_at))
    else:
        source.source_name, source.raw_id, source.evidence, source.observed_at = candidate.source_name, candidate.evidence.raw_id, evidence, candidate.evidence.observed_at
        session.add(source)
    event.source_urls = sorted(set([*event.source_urls, candidate.source_url]))


def upsert_candidate(session: Session, candidate: EventCandidate, assessment: Assessment) -> tuple[Event | None, list[EventChange], bool]:
    """Persist accepted/reviewed candidates and only existing rejected candidates."""
    event = find_existing(session, candidate)
    if event is None and assessment.status == "rejected":
        return None, [], False
    created = event is None
    if event is None:
        event = _new_event(candidate, assessment)
        session.add(event)
        session.flush()
        assert event.id is not None
        changes = [EventChange(event_id=event.id, change_type="new_event", new_value="qualified")]
    else:
        old_values = {field: getattr(event, field) for field in MATERIAL_FIELDS}
        _merge_candidate(event, candidate, assessment)
        session.add(event)
        session.flush()
        assert event.id is not None
        changes = [EventChange(event_id=event.id, change_type=kind, old_value=_text_value(old_values[field]), new_value=_text_value(getattr(event, field))) for field, kind in MATERIAL_FIELDS.items() if _text_value(old_values[field]) != _text_value(getattr(event, field))]
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


def list_events(session: Session, *, text: str | None = None, topic: str | None = None, event_type: str | None = None, event_types: list[str] | None = None, organizer: str | None = None, event_format: str | None = None, registration_state: str | None = None, source: str | None = None, start_after: datetime | None = None, start_before: datetime | None = None, opened_after: datetime | None = None, bengaluru_only: bool = False, status: str | None = None, limit: int = 100) -> list[Event]:
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
        statement = statement.where(Event.starts_at >= start_after)
    if start_before:
        statement = statement.where(Event.starts_at <= start_before)
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


def pending_changes(session: Session) -> list[tuple[EventChange, Event]]:
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
            or _as_utc(event.ends_at or event.starts_at) >= utcnow()
        )
        if visible_eligible or terminal_transition:
            result.append((change, event))
    return result


def mark_changes_digested(session: Session, changes: Iterable[EventChange]) -> None:
    for change in changes:
        change.digested_at = utcnow()
        session.add(change)
    session.commit()
