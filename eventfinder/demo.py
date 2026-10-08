"""Offline reviewer demo backed by synthetic events and a disposable database."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel
from sqlmodel import Session

from eventfinder.config import (
    AIConfig,
    FileConfig,
    OrganizersRegistry,
    PolicyConfig,
    RankingConfig,
    SchedulerConfig,
    ServerConfig,
    Settings,
    SourcesRegistry,
)
from eventfinder.migrations import upgrade_database
from eventfinder.models import Event
from eventfinder.web import create_app

DEMO_HOST = "127.0.0.1"
DEMO_PORT = 18766
DEMO_LABEL = "DEMO DATA — Synthetic examples only. No live sources, model calls, or notifications."
_IST = ZoneInfo("Asia/Kolkata")


class RejectAllHTTPTransport(httpx.AsyncBaseTransport):
    """Fail loudly if any demo code attempts to reach an external service."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        raise RuntimeError(f"Network access is disabled in the reviewer demo: {request.url}")


class OfflineSettings(Settings):
    """Validated Settings construction that bypasses BaseSettings sources."""

    def __init__(self, **data: object) -> None:
        # BaseSettings.__init__ constructs environment and dotenv sources before
        # applying settings_customise_sources. The demo must never inspect them.
        # BaseModel.__init__ still runs Pydantic validation on these explicit
        # values without consulting any external settings source.
        BaseModel.__init__(self, **data)


def _demo_settings(database_url: str, config_dir: Path) -> Settings:
    """Validate explicit, empty credentials without consulting environment or dotenv."""

    return OfflineSettings(
        database_url=database_url,
        telegram_bot_token=None,
        telegram_chat_id=None,
        gemini_api_key=None,
        groq_api_key=None,
        serper_api_key=None,
        brave_search_api_key=None,
        tavily_api_key=None,
        exa_api_key=None,
        config_dir=config_dir,
    )


def _demo_config() -> FileConfig:
    return FileConfig(
        server=ServerConfig(host=DEMO_HOST, port=DEMO_PORT),
        scheduler=SchedulerConfig(),
        policy=PolicyConfig(),
        ai=AIConfig(),
        ranking=RankingConfig(),
        topics={"include": [], "exclude": []},
    )


def _synthetic_events(now: datetime) -> list[Event]:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("demo clock must include a timezone")
    now = now.astimezone(UTC)
    today_ist = now.astimezone(_IST).date()
    examples = (
        (
            "accessible-forms",
            "Testing accessible forms",
            2,
            "workshop",
            "in_person",
            "Bengaluru",
            "Synthetic Community Hall",
            ["accessibility", "testing"],
        ),
        (
            "ai-evaluation",
            "Practical AI evaluation",
            7,
            "talk",
            "online",
            None,
            None,
            ["AI", "evaluation"],
        ),
        (
            "event-pipelines",
            "Reliable event pipelines",
            14,
            "hackathon",
            "hybrid",
            "Bengaluru",
            "Synthetic Technology Centre",
            ["backend", "data"],
        ),
    )
    events: list[Event] = []
    for slug, title, offset, event_type, format, city, venue, topics in examples:
        event_day: date = today_ist + timedelta(days=offset)
        starts_at = datetime.combine(event_day, time(18, 30), _IST).astimezone(UTC)
        events.append(
            Event(
                canonical_url=f"https://example.test/eventfinder/demo/{slug}",
                normalized_key=f"synthetic-demo-{slug}",
                title=f"DEMO · {title}",
                organizer="Synthetic EventFinder Demo",
                concise_summary="A fictional event included only to demonstrate the local dashboard and filters.",
                description="Synthetic reviewer-demo record. This is not a real event listing.",
                starts_at=starts_at,
                venue=venue,
                city=city,
                country="India" if city else None,
                format=format,
                event_type=event_type,
                registration_state="open",
                price_text="Free",
                price_status="free",
                eligibility_text="Synthetic example; no registration is available.",
                topics=topics,
                source_urls=[],
                relevance_reason="Synthetic reviewer demo record; not a real event.",
                organizer_trust="low",
                score=7,
                status="eligible",
                first_seen_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    return events


def create_demo_app(database_path: Path, *, now: datetime | None = None) -> FastAPI:
    """Build the production dashboard around isolated, synthetic demo inputs."""

    database_path = database_path.resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database_url = f"sqlite:///{database_path.as_posix()}"
    config_dir = database_path.parent / "unused-demo-config"
    settings = _demo_settings(database_url, config_dir)
    config = _demo_config()
    source_registry = SourcesRegistry(sources=[])
    organizer_registry = OrganizersRegistry(organizers=[])
    trusted_transport = RejectAllHTTPTransport()
    source_transport = RejectAllHTTPTransport()
    trusted_client = httpx.AsyncClient(transport=trusted_transport)
    source_client = httpx.AsyncClient(transport=source_transport)

    upgrade_database(database_url)
    app = create_app(
        database_url=database_url,
        start_scheduler=False,
        client=trusted_client,
        fetch_client=source_client,
        settings=settings,
        file_config=config,
        sources_registry=source_registry,
        organizers_registry=organizer_registry,
        demo_label=DEMO_LABEL,
        close_injected_clients=True,
    )
    with Session(app.state.engine) as session:
        session.add_all(_synthetic_events(now or datetime.now(UTC)))
        session.commit()

    app.state.demo_database_path = database_path
    app.state.demo_transports = (trusted_transport, source_transport)
    app.state.demo_clients = (trusted_client, source_client)
    app.state.demo_host = DEMO_HOST
    app.state.demo_port = DEMO_PORT
    return app


@contextmanager
def temporary_demo(*, now: datetime | None = None) -> Iterator[FastAPI]:
    """Yield a demo app and remove its SQLite database directory on exit."""

    with TemporaryDirectory(prefix="eventfinder-reviewer-demo-") as directory:
        app = create_demo_app(Path(directory) / "events.sqlite3", now=now)
        app.state.demo_root = Path(directory)
        try:
            yield app
        finally:
            app.state.engine.dispose()


def main() -> None:
    with temporary_demo() as app:
        uvicorn.run(app, host=DEMO_HOST, port=DEMO_PORT, log_level="info")


if __name__ == "__main__":
    main()
