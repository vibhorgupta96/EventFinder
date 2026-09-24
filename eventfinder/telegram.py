"""Idempotent, send-only Telegram delivery with durable cross-midnight recovery."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

import httpx
from sqlmodel import Session, select

from eventfinder.config import Settings
from eventfinder.models import DigestDelivery, DigestRun, Event, EventChange, utcnow
from eventfinder.repository import mark_changes_digested, pending_changes
from eventfinder.urls import safe_outbound_url

IST = ZoneInfo("Asia/Kolkata")
TELEGRAM_SAFE_LIMIT = 4000
MAX_DELIVERY_ATTEMPTS = 3


class TelegramSender(Protocol):
    async def send(self, body: str) -> str: ...


class TelegramHTTPClient:
    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient):
        self.token, self.chat_id, self.client = token, chat_id, client

    async def send(self, body: str) -> str:
        try:
            response = await self.client.post(f"https://api.telegram.org/bot{self.token}/sendMessage", json={"chat_id": self.chat_id, "text": body, "disable_web_page_preview": True}, timeout=20)
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            description = None
            try:
                description = error.response.json().get("description")
            except ValueError:
                description = None
            message = f"Telegram request failed (status {error.response.status_code})"
            if description:
                message += f": {description}"
            raise RuntimeError(message.replace(self.token, "***")) from None
        except httpx.HTTPError as error:
            status = getattr(getattr(error, "response", None), "status_code", None)
            message = "Telegram request failed" + (f" (status {status})" if status else "")
            raise RuntimeError(message.replace(self.token, "***")) from None
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("description", "Telegram rejected the message")).replace(self.token, "***"))
        return str(payload["result"]["message_id"])


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _urgency(event: Event) -> int:
    if not event.registration_deadline:
        return 0
    seconds = (_as_utc(event.registration_deadline) - utcnow()).total_seconds()
    if seconds < 0:
        return 0
    return 4 if seconds <= 2 * 86400 else 2 if seconds <= 7 * 86400 else 0


def _event_order(item: tuple[Event, list[EventChange]]) -> tuple:
    event, _ = item
    bengaluru = int(any(x in " ".join(filter(None, [event.city, event.venue])).casefold() for x in ("bengaluru", "bangalore", "blr")))
    return (-_urgency(event), -bengaluru, -event.score, _as_utc(event.starts_at).timestamp() if event.starts_at else float("inf"), event.title.casefold())


def _time(value: datetime | None) -> str:
    return _as_utc(value).astimezone(IST).strftime("%a, %d %b · %-I:%M %p IST") if value else "Time not stated"


def _capped(value: str | None, limit: int) -> str | None:
    """Truncate a scraped field so one pathological value can't dominate a chunk."""

    if value is None or len(value) <= limit:
        return value
    return value[:limit].rstrip() + "…"


def _format_event(event: Event, changes: list[EventChange]) -> str:
    title = _capped(event.title, 300)
    venue = _capped(event.venue or event.city, 200)
    price_text = _capped(event.price_text, 120)
    lines = [f"• {title}", f"  {_time(event.starts_at)} · {event.format.replace('_', ' ')}"]
    if venue:
        lines.append(f"  {venue}")
    if event.registration_deadline:
        lines.append(f"  Register by {_time(event.registration_deadline)}")
    if event.approval_required:
        lines.append("  Approval required")
    lines.append("  Price not stated" if event.price_status == "not_stated" else "  Free" if event.price_status == "free" else f"  {price_text or 'Paid'}")
    lines.append("  Updated: " + ", ".join(sorted({c.change_type.replace("_", " ") for c in changes})))
    if link := safe_outbound_url(event.registration_url or event.canonical_url):
        lines.append(f"  {link}")
    return "\n".join(lines)


def split_message(text: str, limit: int = TELEGRAM_SAFE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for block in text.split("\n\n"):
        proposed = f"{current}\n\n{block}".strip() if current else block
        if len(proposed) <= limit:
            current = proposed
            continue
        if current:
            chunks.append(current)
        while len(block) > limit:
            point = block.rfind("\n", 0, limit)
            point = limit if point <= 0 else point
            chunks.append(block[:point])
            block = block[point:].lstrip("\n")
        current = block
    if current:
        chunks.append(current)
    return chunks


def _digest_date(now: datetime) -> str:
    return _as_utc(now).astimezone(IST).date().isoformat()


class DigestService:
    def __init__(self, session_factory, sender: TelegramSender | None, limit: int = 10):
        self.session_factory, self.sender, self.limit = session_factory, sender, limit
        self._delivery_lock = asyncio.Lock()

    @staticmethod
    def _retain_committed_entities(session: Session) -> None:
        """Keep digest records usable by callers after an idempotent commit.

        DigestService accepts an injected factory, including a shared session in
        embedding code. SQLAlchemy's default expiration would otherwise detach a
        just-created DigestRun as the service closes that injected session.
        """

        session.expire_on_commit = False

    @staticmethod
    def _oldest_unfinished(session: Session) -> DigestRun | None:
        return session.exec(
            select(DigestRun)
            .where(DigestRun.status.in_(["pending", "partial"]))
            .order_by(DigestRun.started_at)
        ).first()

    @staticmethod
    def _run_for_date(session: Session, digest_date: str) -> DigestRun | None:
        return session.exec(select(DigestRun).where(DigestRun.digest_date == digest_date)).first()

    @staticmethod
    def _run_pairs(session: Session, run: DigestRun) -> list[tuple[EventChange, Event]]:
        pairs = []
        for change_id in run.event_change_ids:
            change = session.get(EventChange, change_id)
            event = session.get(Event, change.event_id) if change else None
            if change and event:
                pairs.append((change, event))
        return pairs

    @staticmethod
    def _group(pairs: list[tuple[EventChange, Event]], limit: int) -> list[tuple[Event, list[EventChange]]]:
        by_event: dict[int, tuple[Event, list[EventChange]]] = {}
        for change, event in pairs:
            by_event.setdefault(event.id, (event, []))[1].append(change)
        return sorted(by_event.values(), key=_event_order)[:limit]

    def _create_run(self, session: Session, now: datetime, changes: list[EventChange]) -> DigestRun:
        run = DigestRun(digest_date=_digest_date(now), event_change_ids=[c.id for c in changes if c.id])
        session.add(run)
        session.commit()
        session.refresh(run)
        return run

    def _deliveries(self, session: Session, run: DigestRun, grouped: list[tuple[Event, list[EventChange]]]) -> list[DigestDelivery]:
        deliveries = list(session.exec(select(DigestDelivery).where(DigestDelivery.digest_run_id == run.id).order_by(DigestDelivery.chunk_index)).all())
        if deliveries:
            return deliveries
        text = "EventFinder: new or changed technical events\n\n" + "\n\n".join(_format_event(event, changes) for event, changes in grouped)
        deliveries = [DigestDelivery(digest_run_id=run.id, chunk_index=index, body=body) for index, body in enumerate(split_message(text))]
        session.add_all(deliveries)
        session.commit()
        return deliveries

    async def _resume(self, session: Session, run: DigestRun, grouped: list[tuple[Event, list[EventChange]]], now: datetime) -> dict[str, object]:
        if run.status == "sent":
            return {"status": "already_sent", "events": len(grouped), "chunks": 0}
        deliveries = self._deliveries(session, run, grouped)
        sent = failures = 0
        for delivery in deliveries:
            if delivery.sent_at or delivery.attempt_count >= MAX_DELIVERY_ATTEMPTS or (
                delivery.next_attempt_at and _as_utc(delivery.next_attempt_at) > now
            ):
                continue
            try:
                delivery.telegram_message_id = await self.sender.send(delivery.body)  # type: ignore[union-attr]
                delivery.sent_at, delivery.error, delivery.next_attempt_at = utcnow(), None, None
                sent += 1
            except (httpx.HTTPError, RuntimeError) as error:
                delivery.attempt_count += 1
                delivery.error = str(error)
                delivery.next_attempt_at = now + timedelta(minutes=min(60, 2 ** delivery.attempt_count))
                failures += 1
            session.add(delivery)
            session.commit()
        pending = [d for d in deliveries if not d.sent_at]
        if pending:
            if all(delivery.attempt_count >= MAX_DELIVERY_ATTEMPTS for delivery in pending):
                # Permanent failure: leave the underlying EventChanges un-digested so
                # they roll into the next day's digest instead of being silently dropped.
                run.status, run.completed_at = "failed", utcnow()
                session.add(run)
                session.commit()
                return {
                    "status": "failed",
                    "events": len(grouped),
                    "chunks": len(deliveries),
                    "sent": sent,
                    "failures": failures,
                }
            run.status = "partial"
            session.add(run)
            session.commit()
            return {"status": "partial", "events": len(grouped), "chunks": len(deliveries), "sent": sent, "failures": failures}
        run.status, run.completed_at = "sent", utcnow()
        session.add(run)
        mark_changes_digested(session, [change for change, _ in self._run_pairs(session, run)])
        return {"status": "sent", "events": len(grouped), "chunks": len(deliveries), "sent": sent}

    async def _send_daily_digest_locked(self, now: datetime) -> dict[str, object]:
        with self.session_factory() as session:
            self._retain_committed_entities(session)
            if self.sender is None:
                return {"status": "not_configured", "events": 0, "chunks": 0}
            digest_date = _digest_date(now)
            unfinished = self._oldest_unfinished(session)
            if unfinished:
                return await self._resume(
                    session,
                    unfinished,
                    self._group(self._run_pairs(session, unfinished), self.limit),
                    now,
                )
            current_run = self._run_for_date(session, digest_date)
            if current_run and current_run.status == "sent":
                return {"status": "already_sent", "events": 0, "chunks": 0}
            if current_run and current_run.status == "failed":
                return {"status": "failed", "events": 0, "chunks": 0}
            pairs = pending_changes(session)
            grouped = self._group(pairs, self.limit)
            selected = [change for _, changes in grouped for change in changes]
            if not selected:
                return {"status": "silent", "events": 0, "chunks": 0}
            run = current_run or self._create_run(session, now, selected)
            return await self._resume(session, run, grouped, now)

    async def send_daily_digest(self, now: datetime | None = None) -> dict[str, object]:
        now = _as_utc(now or utcnow())
        async with self._delivery_lock:
            return await self._send_daily_digest_locked(now)

    async def resume_or_catch_up(self, now: datetime | None = None, digest_hour_ist: int = 9) -> dict[str, object]:
        now = _as_utc(now or utcnow())
        async with self._delivery_lock:
            with self.session_factory() as session:
                self._retain_committed_entities(session)
                unfinished = self._oldest_unfinished(session)
            if unfinished or now.astimezone(IST).hour >= digest_hour_ist:
                return await self._send_daily_digest_locked(now)
            return {"status": "not_due", "events": 0, "chunks": 0}


def make_digest_service(settings: Settings, session_factory, client: httpx.AsyncClient, limit: int) -> DigestService:
    sender = TelegramHTTPClient(settings.telegram_bot_token, settings.telegram_chat_id, client) if settings.telegram_bot_token and settings.telegram_chat_id else None
    return DigestService(session_factory, sender, limit)
