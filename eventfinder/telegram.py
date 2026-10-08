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
from eventfinder.repository import (
    mark_changes_digested,
    notification_allowed,
    pending_changes,
    revalidate_notification_policy,
)
from eventfinder.urls import safe_outbound_url, same_event_destination

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
    if event.registration_state != "unknown":
        lines.append(f"  Registration: {event.registration_state.replace('_', ' ')}")
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


def _digest_chunks(grouped: list[tuple[Event, list[EventChange]]]) -> list[tuple[str, list[int]]]:
    """Split a digest while retaining the changes present in each chunk."""

    header = "EventFinder: new or changed technical events"
    rendered: list[str] = []
    spans: list[tuple[int, int, set[int]]] = []
    offset = len(header) + 2
    for event, changes in grouped:
        body = _format_event(event, changes)
        ids = {change.id for change in changes if change.id is not None}
        spans.append((offset, offset + len(body), ids))
        rendered.append(body)
        offset += len(body) + 2

    text = header + "\n\n" + "\n\n".join(rendered)
    blocks: list[tuple[str, set[int]]] = []
    offset = 0
    for block in text.split("\n\n"):
        end = offset + len(block)
        ids = set().union(*(change_ids for start, stop, change_ids in spans if start < end and stop > offset))
        blocks.append((block, ids))
        offset = end + 2

    chunks: list[tuple[str, list[int]]] = []
    current, current_ids = "", set()
    for original, ids in blocks:
        block = original
        proposed = f"{current}\n\n{block}".strip() if current else block
        if len(proposed) <= TELEGRAM_SAFE_LIMIT:
            current = proposed
            current_ids.update(ids)
            continue
        if current:
            chunks.append((current, sorted(current_ids)))
        while len(block) > TELEGRAM_SAFE_LIMIT:
            point = block.rfind("\n", 0, TELEGRAM_SAFE_LIMIT)
            point = TELEGRAM_SAFE_LIMIT if point <= 0 else point
            chunks.append((block[:point], sorted(ids)))
            block = block[point:].lstrip("\n")
        current, current_ids = block, set(ids)
    if current:
        chunks.append((current, sorted(current_ids)))
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

    @staticmethod
    def _suppress_equivalent_registration_urls(
        session: Session, pairs: list[tuple[EventChange, Event]]
    ) -> list[tuple[EventChange, Event]]:
        """Clear old pending URL changes caused solely by known tracking keys."""

        suppressed = [
            change for change, _ in pairs
            if change.change_type == "registration_url"
            and same_event_destination(change.old_value, change.new_value)
        ]
        if suppressed:
            mark_changes_digested(session, suppressed)
        suppressed_ids = {change.id for change in suppressed}
        return [(change, event) for change, event in pairs if change.id not in suppressed_ids]

    @staticmethod
    def _suppress_reverted_formats(
        session: Session, pairs: list[tuple[EventChange, Event]]
    ) -> list[tuple[EventChange, Event]]:
        """Drop pending format flips whose final value is the original value."""

        by_event: dict[int, list[tuple[EventChange, Event]]] = {}
        for change, event in pairs:
            if change.change_type == "format":
                by_event.setdefault(event.id, []).append((change, event))
        suppressed_ids: set[int] = set()
        suppressed: list[EventChange] = []
        for format_pairs in by_event.values():
            oldest = min(
                format_pairs,
                key=lambda pair: (_as_utc(pair[0].observed_at), pair[0].id or 0),
            )[0]
            if oldest.old_value != format_pairs[0][1].format:
                continue
            for change, _ in format_pairs:
                suppressed.append(change)
                if change.id is not None:
                    suppressed_ids.add(change.id)
        if suppressed:
            mark_changes_digested(session, suppressed)
        return [(change, event) for change, event in pairs if change.id not in suppressed_ids]

    @staticmethod
    def _suppress_reverted_schedules(
        session: Session, pairs: list[tuple[EventChange, Event]]
    ) -> list[tuple[EventChange, Event]]:
        """Drop closed date-change cycles only when one current field confirms them.

        Both starts_at and ends_at are recorded as ``schedule``. Keep a
        connected group when both current fields occur in it, since that can
        represent two real changes with crossing values.
        """

        by_event: dict[int, list[EventChange]] = {}
        events: dict[int, Event] = {}
        for change, event in pairs:
            if change.change_type == "schedule":
                by_event.setdefault(event.id, []).append(change)
                events[event.id] = event
        suppressed: list[EventChange] = []
        for event_id, changes in by_event.items():
            event = events[event_id]
            current = [
                _as_utc(value).isoformat()
                for value in (event.starts_at, event.ends_at)
                if value is not None
            ]
            remaining = changes.copy()
            while remaining:
                values = {remaining[0].old_value, remaining[0].new_value}
                component: list[EventChange] = []
                while connected := [
                    change for change in remaining
                    if change.old_value in values or change.new_value in values
                ]:
                    for change in connected:
                        remaining.remove(change)
                        component.append(change)
                        values.update((change.old_value, change.new_value))
                if None in values:
                    continue
                balance: dict[str, int] = {}
                for change in component:
                    assert change.old_value is not None and change.new_value is not None
                    balance[change.old_value] = balance.get(change.old_value, 0) - 1
                    balance[change.new_value] = balance.get(change.new_value, 0) + 1
                oldest = min(
                    component,
                    key=lambda change: (_as_utc(change.observed_at), change.id or 0),
                )
                if (
                    all(delta == 0 for delta in balance.values())
                    and current.count(oldest.old_value) == 1
                    and not any(value in values for value in current if value != oldest.old_value)
                ):
                    suppressed.extend(component)
        if suppressed:
            mark_changes_digested(session, suppressed)
        suppressed_ids = {change.id for change in suppressed}
        return [(change, event) for change, event in pairs if change.id not in suppressed_ids]

    def _create_run(self, session: Session, now: datetime, changes: list[EventChange]) -> DigestRun:
        run = DigestRun(digest_date=_digest_date(now), event_change_ids=[c.id for c in changes if c.id])
        session.add(run)
        # The run and its frozen delivery bodies must be committed together.
        session.flush()
        return run

    def _failed_carry(
        self, session: Session, digest_date: str, now: datetime
    ) -> tuple[list[DigestDelivery], list[EventChange], set[int]]:
        """Recover unsent frozen chunks from the newest unresolved failed day.

        All failed-run IDs are excluded from fresh rendering until their saved
        chunks have been recovered. A legacy chunk has no attribution, so its
        unsent copies conservatively depend on every unresolved ID in that run.
        """

        excluded: set[int] = set()
        selected: tuple[list[DigestDelivery], list[EventChange]] | None = None
        for prior in session.exec(
            select(DigestRun)
            .where(DigestRun.status == "failed", DigestRun.digest_date < digest_date)
            .order_by(DigestRun.digest_date.desc())
        ):
            pending = [
                change for change_id in prior.event_change_ids
                if (change := session.get(EventChange, change_id)) and change.digested_at is None
            ]
            if not pending:
                continue
            deliveries = list(session.exec(
                select(DigestDelivery)
                .where(DigestDelivery.digest_run_id == prior.id)
                .order_by(DigestDelivery.chunk_index)
            ))
            if not deliveries:
                # Nothing was frozen or sent; the pending changes can be rendered.
                continue
            self._sanitize_deliveries(session, prior, deliveries, now)
            unsent = [delivery for delivery in deliveries if not delivery.sent_at and delivery.body]
            if not unsent:
                valid = [change for change, event in self._run_pairs(session, prior)
                         if change.digested_at is None and notification_allowed(session, event, change, now)]
                if valid:
                    mark_changes_digested(session, valid)
                prior.status = "suppressed" if any(not delivery.body for delivery in deliveries) else "sent"
                prior.completed_at = utcnow()
                session.add(prior)
                session.commit()
                continue
            allowed = {change.id for change, event in self._run_pairs(session, prior)
                       if notification_allowed(session, event, change, now)}
            pending = [change for change in pending if change.id in allowed]
            if not pending:
                # Invalid IDs remain undigested for future factual verification,
                # but cannot keep already-recovered frozen valid bodies alive.
                prior.status, prior.completed_at = "suppressed", utcnow()
                session.add(prior)
                session.commit()
                continue
            excluded.update(change.id for change in pending if change.id is not None)
            if unsent and selected is None:
                selected = (unsent, pending)
        if selected:
            return selected[0], selected[1], excluded
        return [], [], excluded

    @staticmethod
    def _mark_completed_changes(
        session: Session, run: DigestRun, deliveries: list[DigestDelivery]
    ) -> None:
        if not deliveries or any(delivery.event_change_ids is None for delivery in deliveries):
            # Legacy attribution is unknowable unless every saved chunk succeeds.
            if deliveries and all(delivery.sent_at for delivery in deliveries):
                mark_changes_digested(session, [change for change, _ in DigestService._run_pairs(session, run)])
            return
        required: dict[int, list[DigestDelivery]] = {}
        for delivery in deliveries:
            for change_id in delivery.event_change_ids or []:
                required.setdefault(change_id, []).append(delivery)
        completed = [
            change for change_id, chunks in required.items()
            if all(chunk.sent_at for chunk in chunks)
            and (change := session.get(EventChange, change_id))
            and change.digested_at is None
        ]
        if completed:
            mark_changes_digested(session, completed)

    def _deliveries(
        self, session: Session, run: DigestRun,
        grouped: list[tuple[Event, list[EventChange]]],
        carry: list[DigestDelivery] | None = None,
        carry_ids: list[int] | None = None,
    ) -> list[DigestDelivery]:
        deliveries = list(session.exec(select(DigestDelivery).where(DigestDelivery.digest_run_id == run.id).order_by(DigestDelivery.chunk_index)).all())
        if deliveries:
            return deliveries
        frozen = [
            (delivery.body, delivery.event_change_ids if delivery.event_change_ids is not None else carry_ids or [])
            for delivery in (carry or [])
        ]
        chunks = frozen + (_digest_chunks(grouped) if grouped else [])
        deliveries = [
            DigestDelivery(digest_run_id=run.id, chunk_index=index, body=body, event_change_ids=ids)
            for index, (body, ids) in enumerate(chunks)
        ]
        session.add_all(deliveries)
        session.commit()
        return deliveries

    async def _resume(
        self, session: Session, run: DigestRun, grouped: list[tuple[Event, list[EventChange]]],
        now: datetime, carry: list[DigestDelivery] | None = None,
        carry_ids: list[int] | None = None,
    ) -> dict[str, object]:
        event_count = len({event.id for change, event in self._run_pairs(session, run)
                           if notification_allowed(session, event, change, now)})
        if run.status == "sent":
            return {"status": "already_sent", "events": event_count, "chunks": 0}
        deliveries = self._deliveries(session, run, grouped, carry, carry_ids)
        self._sanitize_deliveries(session, run, deliveries, now)
        self._mark_completed_changes(session, run, deliveries)
        sent = failures = 0
        for delivery in deliveries:
            if not delivery.body or delivery.sent_at or delivery.attempt_count >= MAX_DELIVERY_ATTEMPTS or (
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
            self._mark_completed_changes(session, run, deliveries)
        pending = [d for d in deliveries if not d.sent_at and d.body]
        if pending:
            if all(delivery.attempt_count >= MAX_DELIVERY_ATTEMPTS for delivery in pending):
                # Permanent failure: leave the underlying EventChanges un-digested so
                # they roll into the next day's digest instead of being silently dropped.
                run.status, run.completed_at = "failed", utcnow()
                session.add(run)
                session.commit()
                return {
                    "status": "failed",
                    "events": event_count,
                    "chunks": len(deliveries),
                    "sent": sent,
                    "failures": failures,
                }
            run.status = "partial"
            session.add(run)
            session.commit()
            return {"status": "partial", "events": event_count, "chunks": len(deliveries), "sent": sent, "failures": failures}
        if deliveries and all(not delivery.body and not delivery.sent_at for delivery in deliveries):
            run.status, run.completed_at = "suppressed", utcnow()
            session.add(run)
            session.commit()
            return {"status": "suppressed", "events": 0, "chunks": 0, "sent": 0}
        run.status, run.completed_at = "sent", utcnow()
        session.add(run)
        self._mark_completed_changes(session, run, deliveries)
        session.commit()
        return {"status": "sent", "events": event_count, "chunks": len(deliveries), "sent": sent}

    async def _send_daily_digest_locked(self, now: datetime) -> dict[str, object]:
        with self.session_factory() as session:
            self._retain_committed_entities(session)
            revalidate_notification_policy(session, now=now)
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
            if current_run and current_run.status == "suppressed":
                return {"status": "suppressed", "events": 0, "chunks": 0}
            if current_run and current_run.status == "failed":
                return {"status": "failed", "events": 0, "chunks": 0}
            carry, carried_changes, excluded = self._failed_carry(session, digest_date, now)
            pairs = [(change, event) for change, event in pending_changes(session, now) if change.id not in excluded]
            pairs = self._suppress_equivalent_registration_urls(session, pairs)
            pairs = self._suppress_reverted_formats(session, pairs)
            pairs = self._suppress_reverted_schedules(session, pairs)
            carried_events = len({change.event_id for change in carried_changes})
            grouped = self._group(pairs, max(0, self.limit - carried_events))
            selected = [change for _, changes in grouped for change in changes]
            if not selected and not carry:
                return {"status": "silent", "events": 0, "chunks": 0}
            run = current_run or self._create_run(session, now, carried_changes + selected)
            return await self._resume(
                session, run, grouped, now, carry,
                [change.id for change in carried_changes if change.id is not None],
            )

    def _sanitize_deliveries(self, session: Session, run: DigestRun,
                             deliveries: list[DigestDelivery], now: datetime) -> None:
        """Re-render policy-invalid frozen retries; never rewrite sent history."""

        pairs = self._run_pairs(session, run)
        allowed = {change.id for change, event in pairs if notification_allowed(session, event, change, now)}
        unsent = [delivery for delivery in deliveries if not delivery.sent_at and delivery.body]
        if not any(set(delivery.event_change_ids if delivery.event_change_ids is not None
                       else run.event_change_ids) - allowed for delivery in unsent):
            return
        ids = {change_id for delivery in unsent for change_id in (
            delivery.event_change_ids if delivery.event_change_ids is not None else run.event_change_ids
        )}
        valid = [(change, event) for change, event in pairs
                 if change.id in ids & allowed and change.digested_at is None]
        chunks = _digest_chunks(self._group(valid, self.limit)) if valid else []
        for index, delivery in enumerate(unsent):
            delivery.body, delivery.event_change_ids = chunks[index] if index < len(chunks) else ("", [])
            delivery.error = "Revalidated notification policy"
            delivery.attempt_count, delivery.next_attempt_at = 0, None
            session.add(delivery)
        next_index = max((delivery.chunk_index for delivery in deliveries), default=-1) + 1
        for index, (body, change_ids) in enumerate(chunks[len(unsent):]):
            delivery = DigestDelivery(digest_run_id=run.id, chunk_index=next_index + index,
                                      body=body, event_change_ids=change_ids)
            session.add(delivery)
            deliveries.append(delivery)
        session.commit()

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
