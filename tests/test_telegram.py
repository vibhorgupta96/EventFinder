from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from eventfinder.models import DigestDelivery, DigestRun, Event
from eventfinder.policy import assess_candidate
from eventfinder.repository import upsert_candidate
from eventfinder.telegram import DigestService, _urgency, split_message
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
