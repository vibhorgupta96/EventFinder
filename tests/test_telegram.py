from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from eventfinder.models import DigestDelivery, DigestRun, Event, EventChange
from eventfinder.policy import assess_candidate
from eventfinder.repository import upsert_candidate
from eventfinder.telegram import (
    DigestService,
    TelegramHTTPClient,
    _digest_chunks,
    _format_event,
    _urgency,
    split_message,
)
from sqlmodel import select


class Sender:
    def __init__(self, fail_once: bool = False):
        self.bodies: list[str] = []
        self.fail_once = fail_once

    async def send(self, body: str) -> str:
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("temporary Telegram failure")
        self.bodies.append(body)
        return str(len(self.bodies))


@pytest.mark.asyncio
async def test_digest_is_silent_when_no_changes(session):
    service = DigestService(lambda: session, Sender())
    assert (await service.send_daily_digest())["status"] == "silent"


def _seed_format_changes(session, current_format: str, changes: list[tuple[str, str, str]]):
    event = Event(
        canonical_url="https://events.example.test/format-flip",
        normalized_key="format-flip",
        title="Bengaluru Systems Meetup",
        starts_at=datetime.now(UTC) + timedelta(days=8),
        venue="Bengaluru Innovation Center",
        format=current_format,
    )
    session.add(event)
    session.flush()
    rows = [
        EventChange(
            event_id=event.id,
            change_type=kind,
            old_value=old_value,
            new_value=new_value,
            observed_at=datetime(2026, 9, 27, index, tzinfo=UTC),
        )
        for index, (kind, old_value, new_value) in enumerate(changes)
    ]
    session.add_all(rows)
    session.commit()
    return rows


def _seed_schedule_changes(session, starts_at, ends_at, changes):
    event = Event(
        canonical_url="https://events.example.test/schedule-flip",
        normalized_key="schedule-flip",
        title="Bengaluru Systems Meetup",
        starts_at=starts_at,
        ends_at=ends_at,
        venue="Bengaluru Innovation Center",
    )
    session.add(event)
    session.flush()
    rows = [
        EventChange(
            event_id=event.id,
            change_type="schedule",
            old_value=old,
            new_value=new,
            observed_at=datetime(2026, 9, 27, index, tzinfo=UTC),
        )
        for index, (old, new) in enumerate(changes)
    ]
    session.add_all(rows)
    session.commit()
    return event, rows


def _seed_registration_url_changes(session, current_url, changes):
    event = Event(
        canonical_url="https://events.example.test/registration-link",
        normalized_key="registration-link",
        title="Bengaluru Systems Meetup",
        starts_at=datetime.now(UTC) + timedelta(days=8),
        registration_url=current_url,
    )
    session.add(event)
    session.flush()
    rows = [
        EventChange(
            event_id=event.id,
            change_type="registration_url",
            old_value=old,
            new_value=new,
            observed_at=datetime(2026, 9, 27, index, tzinfo=UTC),
        )
        for index, (old, new) in enumerate(changes)
    ]
    session.add_all(rows)
    session.commit()
    return rows


@pytest.mark.asyncio
async def test_pending_meetup_tracking_url_backlog_is_silent_and_cleared(session):
    base = "https://www.meetup.com/python-bengaluru/events/310000001/"
    first = base + "?recId=first"
    second = base + "?recSource=search&searchId=second"
    third = base + "?eventOrigin=home_page"
    rows = _seed_registration_url_changes(
        session, third, [(first, second), (second, third), (third, first), (first, third)]
    )
    sender = Sender()
    service = DigestService(lambda: session, sender)

    assert (await service.send_daily_digest(now=datetime(2026, 9, 28, 3, tzinfo=UTC)))["status"] == "silent"
    assert sender.bodies == []
    assert all(row.digested_at is not None for row in rows)
    assert (await service.send_daily_digest(now=datetime(2026, 9, 29, 3, tzinfo=UTC)))["status"] == "silent"


@pytest.mark.asyncio
async def test_pending_tracking_change_keeps_first_and_functional_registration_links(session):
    base = "https://www.meetup.com/python-bengaluru/events/310000001/"
    first = base + "?recId=first"
    tracked = base + "?recSource=search"
    functional = base + "?recSource=search&ticket=vip"
    rows = _seed_registration_url_changes(
        session, functional, [(None, first), (first, tracked), (tracked, functional)]
    )
    sender = Sender()
    service = DigestService(lambda: session, sender)

    assert (await service.send_daily_digest(now=datetime(2026, 9, 28, 3, tzinfo=UTC)))["status"] == "sent"
    assert session.exec(select(DigestRun)).one().event_change_ids == [rows[0].id, rows[2].id]
    assert "Updated: registration url" in sender.bodies[0]
    assert all(row.digested_at is not None for row in rows)


@pytest.mark.asyncio
async def test_reverted_schedule_chain_is_silent_and_cleared(session):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=8)
    alternate = start + timedelta(hours=1)
    _, rows = _seed_schedule_changes(
        session,
        start,
        None,
        [
            (start.isoformat(), alternate.isoformat()),
            (alternate.isoformat(), start.isoformat()),
            (start.isoformat(), alternate.isoformat()),
            (alternate.isoformat(), start.isoformat()),
        ],
    )
    sender = Sender()
    service = DigestService(lambda: session, sender)

    assert (await service.send_daily_digest(now=datetime(2026, 9, 28, 3, tzinfo=UTC)))["status"] == "silent"
    assert sender.bodies == []
    assert all(row.digested_at is not None for row in rows)
    assert (await service.send_daily_digest(now=datetime(2026, 9, 29, 3, tzinfo=UTC)))["status"] == "silent"


@pytest.mark.asyncio
async def test_reverted_start_does_not_hide_lasting_end_change(session):
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=8)
    alternate = start + timedelta(hours=1)
    old_end = start + timedelta(hours=3)
    new_end = old_end + timedelta(hours=1)
    _, rows = _seed_schedule_changes(
        session,
        start,
        new_end,
        [
            (start.isoformat(), alternate.isoformat()),
            (alternate.isoformat(), start.isoformat()),
            (old_end.isoformat(), new_end.isoformat()),
        ],
    )
    sender = Sender()
    service = DigestService(lambda: session, sender)

    result = await service.send_daily_digest(now=datetime(2026, 9, 28, 3, tzinfo=UTC))
    assert result["status"] == "sent"
    assert "Updated: schedule" in sender.bodies[0]
    run = session.exec(select(DigestRun)).one()
    assert run.event_change_ids == [rows[2].id]
    assert all(row.digested_at is not None for row in rows)


@pytest.mark.asyncio
async def test_crossing_start_end_values_are_not_mistaken_for_reversion(session):
    old_start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=8)
    old_end = old_start + timedelta(hours=1)
    _, rows = _seed_schedule_changes(
        session,
        old_end,
        old_start,
        [
            (old_start.isoformat(), old_end.isoformat()),
            (old_end.isoformat(), old_start.isoformat()),
        ],
    )
    sender = Sender()
    service = DigestService(lambda: session, sender)

    assert (await service.send_daily_digest(now=datetime(2026, 9, 28, 3, tzinfo=UTC)))["status"] == "sent"
    assert session.exec(select(DigestRun)).one().event_change_ids == [row.id for row in rows]
    assert "Updated: schedule" in sender.bodies[0]


@pytest.mark.asyncio
async def test_unfinished_schedule_delivery_keeps_saved_body_after_reversion(session):
    old_start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=8)
    new_start = old_start + timedelta(hours=1)
    event, rows = _seed_schedule_changes(
        session, new_start, None, [(old_start.isoformat(), new_start.isoformat())]
    )
    sender = Sender(fail_once=True)
    service = DigestService(lambda: session, sender)

    assert (await service.send_daily_digest(now=datetime(2026, 9, 28, 3, tzinfo=UTC)))["status"] == "partial"
    run = session.exec(select(DigestRun)).one()
    saved_body = session.exec(select(DigestDelivery)).one().body
    event.starts_at = old_start
    session.add(event)
    session.add(EventChange(
        event_id=event.id,
        change_type="schedule",
        old_value=new_start.isoformat(),
        new_value=old_start.isoformat(),
    ))
    session.commit()

    assert (await service.send_daily_digest(now=datetime(2026, 9, 28, 3, 3, tzinfo=UTC)))["status"] == "sent"
    assert sender.bodies == [saved_body]
    assert "Updated: schedule" in saved_body
    assert run.event_change_ids == [rows[0].id]


@pytest.mark.asyncio
async def test_reverted_format_only_digest_is_silent_and_clears_pending_changes(session):
    rows = _seed_format_changes(
        session,
        "in_person",
        [("format", "in_person", "online"), ("format", "online", "in_person")],
    )
    sender = Sender()
    service = DigestService(lambda: session, sender)

    assert (await service.send_daily_digest(now=datetime(2026, 9, 28, 3, tzinfo=UTC)))["status"] == "silent"
    assert sender.bodies == []
    assert all(row.digested_at is not None for row in rows)
    assert (await service.send_daily_digest(now=datetime(2026, 9, 29, 3, tzinfo=UTC)))["status"] == "silent"


@pytest.mark.asyncio
async def test_reverted_format_does_not_hide_real_change(session):
    rows = _seed_format_changes(
        session,
        "in_person",
        [
            ("format", "in_person", "online"),
            ("format", "online", "in_person"),
            ("schedule", "2026-10-10T04:30:00+00:00", "2026-10-11T04:30:00+00:00"),
        ],
    )
    sender = Sender()
    service = DigestService(lambda: session, sender)

    result = await service.send_daily_digest(now=datetime(2026, 9, 28, 3, tzinfo=UTC))
    assert result["status"] == "sent"
    assert len(sender.bodies) == 1
    assert "Updated: schedule" in sender.bodies[0]
    assert "Updated: format" not in sender.bodies[0]
    run = session.exec(select(DigestRun)).one()
    assert run.event_change_ids == [rows[2].id]
    assert all(row.digested_at is not None for row in rows)


@pytest.mark.asyncio
async def test_real_format_change_is_sent(session):
    rows = _seed_format_changes(session, "online", [("format", "in_person", "online")])
    sender = Sender()
    service = DigestService(lambda: session, sender)

    result = await service.send_daily_digest(now=datetime(2026, 9, 28, 3, tzinfo=UTC))
    assert result["status"] == "sent"
    assert "Updated: format" in sender.bodies[0]
    assert rows[0].digested_at is not None


@pytest.mark.asyncio
async def test_partial_delivery_retries_only_unsent_chunk(session, candidate, config, organizers):
    assessment = await assess_candidate(candidate, config, organizers)
    upsert_candidate(session, candidate, assessment)
    sender = Sender(fail_once=True)
    service = DigestService(lambda: session, sender)
    first = await service.send_daily_digest(now=datetime(2026, 9, 23, 3, tzinfo=UTC))
    assert first["status"] == "partial"
    second = await service.send_daily_digest(now=datetime(2026, 9, 23, 3, 3, tzinfo=UTC))
    assert second["status"] == "sent"
    assert len(sender.bodies) == 1
    assert (await service.send_daily_digest(now=datetime(2026, 9, 23, 3, tzinfo=UTC)))["status"] == "already_sent"


def test_telegram_splitting_preserves_limit():
    chunks = split_message("\n\n".join(["event\n" + "x" * 1000 for _ in range(10)]), limit=1500)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1500 for chunk in chunks)


def test_past_registration_deadline_has_no_telegram_urgency():
    past = Event(
        canonical_url="https://events.example.test/past-deadline",
        normalized_key="past-deadline",
        title="AI workshop",
        registration_deadline=datetime.now(UTC) - timedelta(minutes=1),
    )
    upcoming = Event(
        canonical_url="https://events.example.test/upcoming-deadline",
        normalized_key="upcoming-deadline",
        title="AI workshop",
        registration_deadline=datetime.now(UTC) + timedelta(days=3),
    )
    assert _urgency(past) == 0
    assert _urgency(upcoming) == 2


@pytest.mark.asyncio
async def test_cross_midnight_resume_sends_only_failed_digest_chunks(session, candidate, config, organizers):
    for index in range(10):
        item = candidate.model_copy(deep=True)
        item.title = f"AI engineering event {index} " + ("x" * 600)
        item.canonical_url = f"https://events.example.test/multi-{index}"
        item.source_url = item.canonical_url
        upsert_candidate(session, item, await assess_candidate(item, config, organizers))

    class FailSecondChunk:
        def __init__(self):
            self.calls = 0
            self.sent: list[str] = []

        async def send(self, body: str) -> str:
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("temporary Telegram failure")
            self.sent.append(body)
            return str(self.calls)

    sender = FailSecondChunk()
    service = DigestService(lambda: session, sender)
    first = await service.send_daily_digest(now=datetime(2026, 9, 23, 3, tzinfo=UTC))
    assert first["status"] == "partial"
    run = session.exec(select(DigestRun)).one()
    deliveries = list(
        session.exec(
            select(DigestDelivery)
            .where(DigestDelivery.digest_run_id == run.id)
            .order_by(DigestDelivery.chunk_index)
        )
    )
    successful_before = {
        delivery.id: delivery.telegram_message_id for delivery in deliveries if delivery.sent_at
    }
    assert len(deliveries) > 1
    assert successful_before

    resumed = await service.send_daily_digest(now=datetime(2026, 9, 24, 3, 3, tzinfo=UTC))
    assert resumed["status"] == "sent"
    deliveries = list(
        session.exec(
            select(DigestDelivery)
            .where(DigestDelivery.digest_run_id == run.id)
            .order_by(DigestDelivery.chunk_index)
        )
    )
    assert sender.calls == len(deliveries) + 1
    assert {
        delivery.id: delivery.telegram_message_id
        for delivery in deliveries
        if delivery.id in successful_before
    } == successful_before
    assert all(delivery.sent_at for delivery in deliveries)


@pytest.mark.asyncio
async def test_digest_lock_serializes_concurrent_same_day_delivery(session, candidate, config, organizers):
    upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    sender = Sender()
    service = DigestService(lambda: session, sender)
    results = await asyncio.gather(
        service.send_daily_digest(now=datetime(2026, 9, 23, 3, tzinfo=UTC)),
        service.send_daily_digest(now=datetime(2026, 9, 23, 3, tzinfo=UTC)),
    )
    assert sorted(result["status"] for result in results) == ["already_sent", "sent"]
    assert len(sender.bodies) == 1


@pytest.mark.asyncio
async def test_exhausted_digest_is_observable_and_does_not_block_a_new_day(session, candidate, config, organizers):
    class AlwaysFail:
        async def send(self, _body: str) -> str:
            raise RuntimeError("Telegram unavailable")

    upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    service = DigestService(lambda: session, AlwaysFail())
    assert (await service.send_daily_digest(now=datetime(2026, 9, 23, 3, tzinfo=UTC)))["status"] == "partial"
    assert (await service.send_daily_digest(now=datetime(2026, 9, 23, 3, 3, tzinfo=UTC)))["status"] == "partial"
    assert (await service.send_daily_digest(now=datetime(2026, 9, 23, 3, 7, tzinfo=UTC)))["status"] == "failed"
    assert session.exec(select(DigestRun).where(DigestRun.digest_date == "2026-09-23")).one().status == "failed"

    changed = candidate.model_copy(deep=True)
    changed.canonical_url = "https://events.example.test/new-day"
    changed.source_url = changed.canonical_url
    changed.title = "Bengaluru AI new day workshop"
    upsert_candidate(session, changed, await assess_candidate(changed, config, organizers))
    assert (await service.send_daily_digest(now=datetime(2026, 9, 24, 3, tzinfo=UTC)))["status"] == "partial"
    assert len(list(session.exec(select(DigestRun)))) == 2


@pytest.mark.asyncio
async def test_failed_run_does_not_drop_changes(session, candidate, config, organizers):
    """A permanently failed run must leave EventChanges un-digested so they roll forward."""

    class AlwaysFail:
        async def send(self, _body: str) -> str:
            raise RuntimeError("Telegram unavailable")

    upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    service = DigestService(lambda: session, AlwaysFail())
    assert (await service.send_daily_digest(now=datetime(2026, 9, 23, 3, tzinfo=UTC)))["status"] == "partial"
    assert (await service.send_daily_digest(now=datetime(2026, 9, 23, 3, 3, tzinfo=UTC)))["status"] == "partial"
    failed = await service.send_daily_digest(now=datetime(2026, 9, 23, 3, 7, tzinfo=UTC))
    assert failed["status"] == "failed"

    still_pending = list(session.exec(select(EventChange)))
    assert still_pending
    assert all(change.digested_at is None for change in still_pending)
    pending_ids = {change.id for change in still_pending}

    service.sender = Sender()
    resumed = await service.send_daily_digest(now=datetime(2026, 9, 24, 3, tzinfo=UTC))
    assert resumed["status"] == "sent"

    next_run = session.exec(select(DigestRun).where(DigestRun.digest_date == "2026-09-24")).one()
    assert pending_ids <= set(next_run.event_change_ids)

    digested = list(session.exec(select(EventChange)))
    assert all(change.digested_at is not None for change in digested)


@pytest.mark.asyncio
async def test_telegram_send_never_leaks_bot_token(session, candidate, config, organizers):
    token = "123456789:AA-Secret-Bot-Token-Value"  # noqa: S105 - fixture value, not a real credential

    def handler(request: httpx.Request) -> httpx.Response:
        assert token in str(request.url)
        return httpx.Response(400, json={"ok": False, "description": "Bad Request: chat not found"})

    upsert_candidate(session, candidate, await assess_candidate(candidate, config, organizers))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sender = TelegramHTTPClient(token, "chat-id", client)

        with pytest.raises(RuntimeError) as exc_info:
            await sender.send("hello")
        message = str(exc_info.value)
        assert token not in message
        assert "400" in message
        assert "chat not found" in message

        service = DigestService(lambda: session, sender)
        result = await service.send_daily_digest(now=datetime(2026, 9, 23, 3, tzinfo=UTC))

    assert result["status"] == "partial"
    delivery = session.exec(select(DigestDelivery)).one()
    assert delivery.error is not None
    assert token not in delivery.error
    assert "400" in delivery.error


def test_split_message_hard_slices_newline_less_over_long_block():
    block = "x" * 5000
    chunks = split_message(block, limit=1000)
    assert len(chunks) == 5
    assert all(len(chunk) == 1000 for chunk in chunks)
    assert "".join(chunks) == block


def test_format_event_truncates_overlong_fields():
    event = Event(
        canonical_url="https://events.example.test/overlong",
        normalized_key="overlong",
        title="T" * 500,
        venue="V" * 400,
        price_text="P" * 300,
        price_status="paid",
    )
    body = _format_event(event, [])
    assert "T" * 500 not in body
    assert "V" * 400 not in body
    assert "P" * 300 not in body
    assert "…" in body
    first_line = body.splitlines()[0]
    assert len(first_line) <= len("• ") + 301


def _seed_long_digest_events(session, count: int = 8):
    rows = []
    for index in range(count):
        event = Event(
            canonical_url=f"https://events.example.test/long-{index}",
            normalized_key=f"long-{index}",
            title=f"Long event {index} " + "T" * 300,
            venue="V" * 200,
            price_status="paid",
            price_text="P" * 120,
            registration_url=f"https://events.example.test/register/{index}?ticket=" + "X" * 550,
            starts_at=datetime.now(UTC) + timedelta(days=8),
        )
        session.add(event)
        session.flush()
        change = EventChange(event_id=event.id, change_type="new", new_value="discovered")
        session.add(change)
        rows.append(change)
    session.commit()
    return rows


@pytest.mark.asyncio
async def test_exhausted_three_chunk_digest_carries_only_unsent_bodies(session):
    rows = _seed_long_digest_events(session)

    class FirstOnly:
        def __init__(self):
            self.calls: list[str] = []
            self.recovered = False

        async def send(self, body: str) -> str:
            self.calls.append(body)
            if len(self.calls) > 1 and not self.recovered:
                raise RuntimeError("Telegram unavailable")
            return str(len(self.calls))

    sender = FirstOnly()
    service = DigestService(lambda: session, sender)
    day1 = datetime(2026, 9, 23, 3, tzinfo=UTC)
    assert (await service.send_daily_digest(now=day1))["status"] == "partial"
    first_run = session.exec(select(DigestRun)).one()
    chunks = list(session.exec(select(DigestDelivery).order_by(DigestDelivery.chunk_index)))
    assert len(chunks) == 3
    first_body = chunks[0].body
    assert chunks[0].sent_at is not None
    assert all(row.digested_at is not None for row in rows if row.id in chunks[0].event_change_ids)
    assert all(row.digested_at is None for row in rows if row.id in chunks[1].event_change_ids + chunks[2].event_change_ids)
    assert (await service.send_daily_digest(now=day1 + timedelta(minutes=3)))["status"] == "partial"
    assert (await service.send_daily_digest(now=day1 + timedelta(minutes=7)))["status"] == "failed"
    assert session.get(DigestRun, first_run.id).status == "failed"

    carried_event_count = len({row.event_id for row in rows if row.digested_at is None})
    sender.recovered = True
    result = await service.send_daily_digest(now=day1 + timedelta(days=1))
    assert result["status"] == "sent"
    assert result["events"] == carried_event_count
    assert sender.calls.count(first_body) == 1
    next_run = session.exec(select(DigestRun).where(DigestRun.digest_date == "2026-09-24")).one()
    carried = list(session.exec(
        select(DigestDelivery).where(DigestDelivery.digest_run_id == next_run.id)
        .order_by(DigestDelivery.chunk_index)
    ))
    assert [delivery.body for delivery in carried] == [chunk.body for chunk in chunks[1:]]
    assert all(session.get(EventChange, row.id).digested_at is not None for row in rows)


@pytest.mark.asyncio
async def test_event_change_spanning_chunks_clears_only_after_final_chunk(session):
    event = Event(
        canonical_url="https://events.example.test/spanning",
        normalized_key="spanning",
        title="Spanning event",
        starts_at=datetime.now(UTC) + timedelta(days=8),
        registration_url="https://events.example.test/register?ticket=" + "X" * 8500,
    )
    session.add(event)
    session.flush()
    change = EventChange(event_id=event.id, change_type="new", new_value="discovered")
    session.add(change)
    session.commit()

    class FailLast:
        def __init__(self):
            self.calls = 0
            self.bodies: list[str] = []
            self.recovered = False

        async def send(self, body: str) -> str:
            self.calls += 1
            self.bodies.append(body)
            if self.calls >= 3 and not self.recovered:
                raise RuntimeError("last chunk failed")
            return str(self.calls)

    sender = FailLast()
    service = DigestService(lambda: session, sender)
    day1 = datetime(2026, 9, 23, 3, tzinfo=UTC)
    assert (await service.send_daily_digest(now=day1))["status"] == "partial"
    deliveries = list(session.exec(select(DigestDelivery).order_by(DigestDelivery.chunk_index)))
    assert len(deliveries) >= 3
    assert all(change.id in delivery.event_change_ids for delivery in deliveries[1:])
    assert change.digested_at is None
    assert (await service.send_daily_digest(now=day1 + timedelta(minutes=3)))["status"] == "partial"
    assert (await service.send_daily_digest(now=day1 + timedelta(minutes=7)))["status"] == "failed"
    sender.recovered = True
    assert (await service.send_daily_digest(now=day1 + timedelta(days=1)))["status"] == "sent"
    assert all(sender.bodies.count(delivery.body) == 1 for delivery in deliveries if delivery.sent_at)
    assert session.get(EventChange, change.id).digested_at is not None


@pytest.mark.asyncio
async def test_legacy_failed_chunks_carry_frozen_unsent_body_across_failed_days(session):
    event = Event(
        canonical_url="https://events.example.test/legacy",
        normalized_key="legacy",
        title="Old title",
        starts_at=datetime.now(UTC) + timedelta(days=8),
    )
    session.add(event)
    session.flush()
    old = EventChange(event_id=event.id, change_type="new", new_value="discovered")
    session.add(old)
    session.flush()
    legacy = DigestRun(digest_date="2026-09-23", status="failed", event_change_ids=[old.id])
    session.add(legacy)
    session.flush()
    session.add_all([
        DigestDelivery(
            digest_run_id=legacy.id, chunk_index=0, body="OLD SENT BODY",
            sent_at=datetime.now(UTC), telegram_message_id="100",
        ),
        DigestDelivery(
            digest_run_id=legacy.id, chunk_index=1, body="OLD UNSENT BODY",
            attempt_count=3,
        ),
    ])
    event.title = "Changed title"
    session.add(event)
    fresh_event = Event(
        canonical_url="https://events.example.test/fresh-after-legacy",
        normalized_key="fresh-after-legacy",
        title="Fresh event",
        starts_at=datetime.now(UTC) + timedelta(days=8),
    )
    session.add(fresh_event)
    session.flush()
    fresh_change = EventChange(event_id=fresh_event.id, change_type="new", new_value="discovered")
    session.add(fresh_change)
    session.commit()

    class FailFrozen:
        def __init__(self):
            self.calls: list[str] = []
            self.recovered = False

        async def send(self, body: str) -> str:
            self.calls.append(body)
            if body == "OLD UNSENT BODY" and not self.recovered:
                raise RuntimeError("still unavailable")
            return str(len(self.calls))

    sender = FailFrozen()
    service = DigestService(lambda: session, sender, limit=2)
    day2 = datetime(2026, 9, 24, 3, tzinfo=UTC)
    first = await service.send_daily_digest(now=day2)
    assert first["status"] == "partial"
    assert first["events"] == 2
    assert sum("Fresh event" in body for body in sender.calls) == 1
    assert session.get(EventChange, fresh_change.id).digested_at is not None
    retry = await service.send_daily_digest(now=day2 + timedelta(minutes=3))
    assert retry["status"] == "partial"
    assert retry["events"] == first["events"]
    exhausted = await service.send_daily_digest(now=day2 + timedelta(minutes=7))
    assert exhausted["status"] == "failed"
    assert exhausted["events"] == first["events"]
    assert old.digested_at is None
    sender.recovered = True
    carry_only = await service.send_daily_digest(now=day2 + timedelta(days=2))
    assert carry_only["status"] == "sent"
    assert carry_only["events"] == 1
    assert sender.calls.count("OLD SENT BODY") == 0
    assert sender.calls.count("OLD UNSENT BODY") == 4
    assert all("Changed title" not in body for body in sender.calls)
    assert sum("Fresh event" in body for body in sender.calls) == 1
    assert session.get(EventChange, old.id).digested_at is not None


@pytest.mark.parametrize("state", ["cancelled", "postponed", "sold_out", "closed"])
def test_digest_displays_current_terminal_registration_state(state):
    event = Event(
        canonical_url="https://events.example.test/terminal",
        normalized_key="terminal",
        title="Terminal event",
        registration_state=state,
    )
    assert f"Registration: {state.replace('_', ' ')}" in _format_event(event, [])


@pytest.mark.asyncio
async def test_cancelled_event_digest_states_cancellation_explicitly(session):
    event = Event(
        canonical_url="https://events.example.test/cancelled",
        normalized_key="cancelled",
        title="Cancelled event",
        starts_at=datetime.now(UTC) + timedelta(days=8),
        registration_state="cancelled",
        status="ineligible",
    )
    session.add(event)
    session.flush()
    session.add(EventChange(
        event_id=event.id,
        change_type="registration_state",
        old_value="open",
        new_value="cancelled",
    ))
    session.commit()
    sender = Sender()
    result = await DigestService(lambda: session, sender).send_daily_digest(
        now=datetime(2026, 9, 23, 3, tzinfo=UTC)
    )
    assert result["status"] == "sent"
    assert "Registration: cancelled" in sender.bodies[0]


def test_digest_chunk_mapping_preserves_existing_split_text(session):
    rows = _seed_long_digest_events(session)
    grouped = [(session.get(Event, row.event_id), [row]) for row in rows]
    grouped[0][0].title = "A\n\nB " + "T" * 300
    text = "EventFinder: new or changed technical events\n\n" + "\n\n".join(
        _format_event(event, changes) for event, changes in grouped
    )
    assert [body for body, _ in _digest_chunks(grouped)] == split_message(text)
