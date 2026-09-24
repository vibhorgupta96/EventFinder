"""Read-only FastAPI dashboard and JSON interfaces."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlmodel import Session, select

from eventfinder.ai import make_classifier
from eventfinder.config import (
    get_file_config,
    get_organizers_registry,
    get_settings,
    get_sources_registry,
)
from eventfinder.db import make_engine
from eventfinder.models import Event
from eventfinder.repository import list_events, source_health
from eventfinder.scheduler import EventFinderScheduler
from eventfinder.service import DiscoveryService
from eventfinder.telegram import make_digest_service
from eventfinder.urls import URLSafeTransport, safe_outbound_url

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))


def _ist_display(value: datetime | None) -> str | None:
    if value is None:
        return None
    timestamp = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return timestamp.astimezone(ZoneInfo("Asia/Kolkata")).strftime(
        "%a, %d %b · %-I:%M %p IST"
    )


def _event_payload(event: Event) -> dict[str, object]:
    return {
        "id": event.id,
        "title": event.title,
        "organizer": event.organizer,
        "summary": event.concise_summary or event.description,
        "starts_at": event.starts_at,
        "starts_at_ist": _ist_display(event.starts_at),
        "ends_at": event.ends_at,
        "venue": event.venue,
        "city": event.city,
        "format": event.format,
        "event_type": event.event_type,
        "registration_state": event.registration_state,
        "registration_url": safe_outbound_url(event.registration_url),
        "registration_deadline": event.registration_deadline,
        "registration_deadline_ist": _ist_display(event.registration_deadline),
        "price_text": event.price_text,
        "price_status": event.price_status,
        "eligibility_text": event.eligibility_text,
        "approval_required": event.approval_required,
        "speakers": event.speakers,
        "topics": event.topics,
        "source_urls": [url for value in event.source_urls if (url := safe_outbound_url(value))],
        "official_url": safe_outbound_url(event.canonical_url),
        "score": event.score,
        "status": event.status,
        "relevance_reason": event.relevance_reason,
    }


def _sections(session: Session, limit: int) -> dict[str, list[Event]]:
    now = datetime.now(UTC)
    return {
        "Upcoming": list_events(session, limit=limit),
        "Registration opened": list_events(session, opened_after=now - timedelta(days=7), limit=limit),
        "Online": list_events(session, event_format="online", limit=limit),
        "Bengaluru": list_events(session, bengaluru_only=True, limit=limit),
        "Hackathons": list_events(
            session, event_types=["hackathon", "buildathon", "competition"], limit=limit
        ),
        "Needs review": list_events(session, status="needs_review", limit=limit),
    }


def create_app(
    *,
    database_url: str | None = None,
    start_scheduler: bool = True,
    client: httpx.AsyncClient | None = None,
    fetch_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = get_settings()
    file_config = get_file_config()
    sources = get_sources_registry()
    organizers = get_organizers_registry()
    engine = make_engine(database_url)
    managed_client = client is None
    managed_fetch_client = fetch_client is None
    # Trusted, fixed-destination integrations (Telegram, Gemini/Groq) never
    # need DNS pinning; only discovery fetches arbitrary public-source URLs
    # and therefore gets the SSRF-hardened, DNS-pinned transport.
    http_client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0))
    # URLSafeTransport pins every request's connection to a validated IP, but
    # httpcore keys keepalive pool reuse by (scheme, host, port) using that
    # pinned IP and ignores the `sni_hostname` extension. Two distinct source
    # hostnames that share an IP (e.g. behind a common CDN) could otherwise
    # have a keepalive TLS connection negotiated/verified for host A reused
    # for a request to host B, letting B ride a TLS channel authenticated for
    # A. Disabling keepalive reuse forces a fresh, freshly-verified
    # connection per request instead. This must be applied to the transport
    # actually performing the pinned request (URLSafeTransport's inner
    # transport) since passing `limits=` to AsyncClient has no effect once a
    # custom `transport=` is supplied.
    discovery_limits = httpx.Limits(max_connections=20, max_keepalive_connections=0)
    discovery_client = fetch_client or httpx.AsyncClient(
        transport=URLSafeTransport(inner=httpx.AsyncHTTPTransport(limits=discovery_limits)),
        timeout=httpx.Timeout(20.0),
        limits=discovery_limits,
    )

    def session_factory() -> Session:
        return Session(engine)

    classifier = make_classifier(settings, file_config.ai, http_client)
    discovery = DiscoveryService(session_factory, file_config, sources, organizers, discovery_client, classifier)
    digest = make_digest_service(settings, session_factory, http_client, file_config.ranking.digest_limit)
    scheduler = EventFinderScheduler(discovery, digest, file_config.scheduler)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        startup_task: asyncio.Task[None] | None = None
        if start_scheduler:
            scheduler.start()
            # Let ASGI begin accepting requests before the bounded startup
            # refresh; the scheduler owns subsequent discovery work.
            startup_task = asyncio.create_task(scheduler.startup())
        try:
            yield
        finally:
            if startup_task and not startup_task.done():
                startup_task.cancel()
                try:
                    await startup_task
                except asyncio.CancelledError:
                    pass
            scheduler.shutdown()
            if managed_client:
                await http_client.aclose()
            if managed_fetch_client:
                await discovery_client.aclose()

    app = FastAPI(title="EventFinder", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.scheduler = scheduler
    app.state.discovery = discovery
    app.state.digest = digest
    app.mount("/static", StaticFiles(directory=str(PACKAGE_DIR / "static")), name="static")

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        with session_factory() as session:
            sections = _sections(session, file_config.policy.dashboard_limit)
            health = source_health(session)
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "sections": {name: [_event_payload(event) for event in events] for name, events in sections.items()},
                "source_health": health,
            },
        )

    @app.get("/api/events")
    def events_api(
        text: str | None = None,
        topic: str | None = None,
        event_type: str | None = None,
        organizer: str | None = None,
        format: str | None = None,
        registration_state: str | None = None,
        source: str | None = None,
        start_after: datetime | None = None,
        start_before: datetime | None = None,
        status: str | None = Query(default=None, pattern="^(eligible|needs_review)$"),
    ):
        with session_factory() as session:
            events = list_events(
                session,
                text=text,
                topic=topic,
                event_type=event_type,
                organizer=organizer,
                event_format=format,
                registration_state=registration_state,
                source=source,
                start_after=start_after,
                start_before=start_before,
                status=status,
                limit=file_config.policy.dashboard_limit,
            )
        return {"events": [_event_payload(event) for event in events]}

    @app.get("/api/sources")
    def sources_api():
        with session_factory() as session:
            return {"sources": source_health(session)}

    @app.get("/healthz")
    def healthz():
        now = datetime.now(UTC)
        try:
            with session_factory() as session:
                session.exec(select(Event.id).limit(1)).first()
                health = source_health(session)
        except Exception:
            # Never leak internal exception text/paths in a public health
            # response; a static per-component label is sufficient signal.
            logger.exception("Health check database probe failed")
            return JSONResponse(
                status_code=503,
                content={"status": "degraded", "database": "error", "scheduler": scheduler.running},
            )
        priority_names = {source.name for source in sources.sources if source.priority and source.enabled}
        fresh_priority = {
            item["source_name"]
            for item in health
            if item["source_name"] in priority_names
            and item["status"] == "ok"
            and item["last_finished_at"]
            and (
                item["last_finished_at"].replace(tzinfo=UTC)
                if item["last_finished_at"].tzinfo is None
                else item["last_finished_at"].astimezone(UTC)
            )
            >= now - timedelta(hours=file_config.scheduler.discovery_hours * 2)
        }
        healthy = scheduler.running and bool(fresh_priority)
        payload = {
            "status": "ok" if healthy else "degraded",
            "database": "ok",
            "scheduler": scheduler.running,
            "priority_sources_fresh": len(fresh_priority),
            "priority_sources_total": len(priority_names),
        }
        return payload if healthy else JSONResponse(status_code=503, content=payload)

    return app
