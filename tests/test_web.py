from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from eventfinder.config import SourceDefinition, SourcesRegistry, get_sources_registry
from eventfinder.migrations import upgrade_database
from eventfinder.models import Event, SourceRun
from eventfinder.policy import assess_candidate
from eventfinder.repository import finish_source_run, start_source_run, upsert_candidate
from eventfinder.web import create_app
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlmodel import Session


@pytest.mark.asyncio
async def test_read_only_dashboard_and_event_filters(tmp_path, candidate, config, organizers):
    database_url = f"sqlite:///{tmp_path / 'web.sqlite3'}"
    upgrade_database(database_url)
    app = create_app(database_url=database_url, start_scheduler=False)
    assessment = await assess_candidate(candidate, config, organizers)
    with Session(app.state.engine) as session:
        upsert_candidate(session, candidate, assessment)
    with TestClient(app) as client:
        dashboard = client.get("/")
        response = client.get("/api/events", params={"topic": "ai", "format": "in_person"})
        assert dashboard.status_code == 200
        assert "Bengaluru AI Systems Meetup" in dashboard.text
        assert response.status_code == 200
        assert response.json()["events"][0]["title"] == candidate.title
        assert client.post("/api/events").status_code == 405


def _dated_app(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'date-filter.sqlite3'}"
    upgrade_database(database_url)
    app = create_app(database_url=database_url, start_scheduler=False)
    day = datetime.now(UTC).date() + timedelta(days=10)
    midnight = datetime.combine(day, time.min, ZoneInfo("Asia/Kolkata"))
    times = {
        "before": midnight - timedelta(microseconds=1),
        "start": midnight,
        "middle": midnight + timedelta(hours=12),
        "last": midnight + timedelta(days=1, microseconds=-1),
        "next": midnight + timedelta(days=1),
    }
    with Session(app.state.engine) as session:
        for title, starts_at in times.items():
            session.add(Event(
                title=title, canonical_url=f"https://events.example.test/{title}",
                normalized_key=title, starts_at=starts_at, status="eligible",
            ))
        session.commit()
    return app, day, midnight


def test_date_filters_cover_the_entire_ist_day(tmp_path):
    app, day, _ = _dated_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/api/events", params={"start_after": day.isoformat(), "start_before": day.isoformat()})
    assert response.status_code == 200
    assert {event["title"] for event in response.json()["events"]} == {"start", "middle", "last"}


def test_aware_timestamp_filters_are_normalized_and_inclusive(tmp_path):
    app, _, midnight = _dated_app(tmp_path)
    timestamp = midnight.astimezone(ZoneInfo("America/Los_Angeles")).isoformat()
    with TestClient(app) as client:
        response = client.get("/api/events", params={"start_after": timestamp, "start_before": timestamp})
    assert response.status_code == 200
    assert [event["title"] for event in response.json()["events"]] == ["start"]


@pytest.mark.parametrize("value", ["not-a-date", "2026-02-30", "2026-10-12T18:00:00", "2026-10-12T18:00:00+25:00", "9999-12-31"])
def test_malformed_date_filter_returns_422(tmp_path, value):
    database_url = f"sqlite:///{tmp_path / 'bad-date.sqlite3'}"
    upgrade_database(database_url)
    app = create_app(database_url=database_url, start_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/api/events", params={"start_before": value}).status_code == 422


@pytest.mark.asyncio
async def test_healthz_reports_503_when_scheduler_and_priority_sources_are_stale(tmp_path):
    """With no scheduler running and no recorded source runs, the service is
    genuinely degraded, so /healthz must not lie by returning 200."""
    database_url = f"sqlite:///{tmp_path / 'healthz-degraded.sqlite3'}"
    upgrade_database(database_url)
    app = create_app(database_url=database_url, start_scheduler=False)
    with TestClient(app) as client:
        health = client.get("/healthz")
    assert health.status_code == 503
    body = health.json()
    assert body["status"] == "degraded"
    assert body["scheduler"] is False


@pytest.mark.asyncio
async def test_healthz_returns_200_when_scheduler_and_priority_sources_are_healthy(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'healthz-healthy.sqlite3'}"
    upgrade_database(database_url)
    app = create_app(database_url=database_url, start_scheduler=False)
    with Session(app.state.engine) as session:
        for source in get_sources_registry().sources:
            if source.enabled and source.priority:
                run = start_source_run(session, source.name)
                finish_source_run(session, run, fetched_count=3, accepted_count=1)
    monkeypatch.setattr(type(app.state.scheduler), "running", property(lambda self: True))
    with TestClient(app) as client:
        health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


@pytest.mark.parametrize(("total", "fresh", "expected"), [(10, 1, 503), (10, 5, 200), (11, 5, 503), (11, 6, 200), (1, 1, 200), (0, 0, 503)])
def test_healthz_requires_priority_source_quorum(tmp_path, monkeypatch, total, fresh, expected):
    definitions = [SourceDefinition(name=f"priority_{index}", adapter="public_page", url=f"https://source{index}.example.test/", priority=True) for index in range(total)]
    monkeypatch.setattr("eventfinder.web.get_sources_registry", lambda: SourcesRegistry(sources=definitions))
    database_url = f"sqlite:///{tmp_path / 'quorum.sqlite3'}"
    upgrade_database(database_url)
    app = create_app(database_url=database_url, start_scheduler=False)
    with Session(app.state.engine) as session:
        for source in definitions[:fresh]:
            run = start_source_run(session, source.name)
            finish_source_run(session, run, fetched_count=1)
    monkeypatch.setattr(type(app.state.scheduler), "running", property(lambda self: True))
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == expected
    payload = response.json()
    assert payload["priority_sources_total"] == total
    assert payload["priority_sources_fresh"] == fresh
    assert payload["priority_sources_required"] == max(1, (total + 1) // 2)
    assert len(payload["priority_sources"]) == total


def test_healthz_freshness_uses_each_enabled_source_cadence(tmp_path, monkeypatch):
    definitions = [
        SourceDefinition(name="frequent", adapter="public_page", url="https://fast.example.test/", priority=True, cadence_hours=3),
        SourceDefinition(name="slow", adapter="public_page", url="https://slow.example.test/", priority=True, cadence_hours=12),
        SourceDefinition(name="disabled", adapter="public_page", url="https://off.example.test/", priority=True, enabled=False),
    ]
    monkeypatch.setattr("eventfinder.web.get_sources_registry", lambda: SourcesRegistry(sources=definitions))
    database_url = f"sqlite:///{tmp_path / 'cadence.sqlite3'}"
    upgrade_database(database_url)
    app = create_app(database_url=database_url, start_scheduler=False)
    old = datetime.now(UTC) - timedelta(hours=10)
    with Session(app.state.engine) as session:
        session.add_all([SourceRun(source_name=source.name, started_at=old, finished_at=old, fetched_count=1) for source in definitions])
        session.commit()
    monkeypatch.setattr(type(app.state.scheduler), "running", property(lambda self: True))
    with TestClient(app) as client:
        response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["priority_sources_total"] == 2
    assert payload["priority_sources_fresh"] == 1
    assert {item["source_name"]: item["state"] for item in payload["priority_sources"]} == {"frequent": "stale", "slow": "fresh"}


@pytest.mark.asyncio
async def test_healthz_does_not_leak_database_error_detail(tmp_path, monkeypatch):
    database_url = f"sqlite:///{tmp_path / 'healthz-db-error.sqlite3'}"
    upgrade_database(database_url)
    app = create_app(database_url=database_url, start_scheduler=False)

    def _broken_source_health(_session):
        raise RuntimeError("secret-internal-path=/etc/eventfinder/db.sqlite3")

    monkeypatch.setattr("eventfinder.web.source_health", _broken_source_health)
    with TestClient(app) as client:
        health = client.get("/healthz")
    assert health.status_code == 503
    assert "secret-internal-path" not in health.text
    body = health.json()
    assert body["status"] == "degraded"
    assert body["database"] == "error"


def test_discovery_client_disables_keepalive_reuse_for_pinned_connections(tmp_path):
    """URLSafeTransport pins every discovery request to a validated IP, but
    httpcore keys keepalive pool reuse by (scheme, host, port) using that
    pinned IP and ignores the ``sni_hostname`` extension. Two distinct source
    hostnames sharing an IP (e.g. a common CDN) could otherwise reuse a
    keepalive TLS connection verified for one host on a request to the
    other. Keepalive reuse must be disabled on the pinned discovery
    transport (not merely passed as an ``AsyncClient(limits=...)`` kwarg,
    which is silently ignored once a custom ``transport=`` is supplied)."""
    database_url = f"sqlite:///{tmp_path / 'discovery-limits.sqlite3'}"
    upgrade_database(database_url)
    app = create_app(database_url=database_url, start_scheduler=False)
    pool = app.state.discovery.client._transport.inner._pool
    assert pool._max_keepalive_connections == 0
    assert pool._max_connections == 20


@pytest.mark.asyncio
async def test_injected_fetch_client_is_preserved_and_not_closed(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'injected-client.sqlite3'}"
    upgrade_database(database_url)
    fetch_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    app = create_app(database_url=database_url, start_scheduler=False, fetch_client=fetch_client)
    with TestClient(app):
        assert app.state.discovery.client is fetch_client
    assert not fetch_client.is_closed
    await fetch_client.aclose()


def test_clean_temp_database_is_created_by_alembic_only(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'nested' / 'migrated.sqlite3'}"
    upgrade_database(database_url)
    app = create_app(database_url=database_url, start_scheduler=False)
    with Session(app.state.engine) as session:
        assert session.exec(text("SELECT version_num FROM alembic_version")).one()[0] == "0002_digest_delivery_change_ids"
