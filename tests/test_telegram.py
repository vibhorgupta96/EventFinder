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
