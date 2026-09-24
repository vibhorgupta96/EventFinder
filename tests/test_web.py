from __future__ import annotations

import pytest
from eventfinder.migrations import upgrade_database
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
        run = start_source_run(session, "gdg_bengaluru")
        finish_source_run(session, run, fetched_count=3, accepted_count=1)
    monkeypatch.setattr(type(app.state.scheduler), "running", property(lambda self: True))
    with TestClient(app) as client:
        health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"


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


def test_clean_temp_database_is_created_by_alembic_only(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'nested' / 'migrated.sqlite3'}"
    upgrade_database(database_url)
    app = create_app(database_url=database_url, start_scheduler=False)
    with Session(app.state.engine) as session:
        assert session.exec(text("SELECT version_num FROM alembic_version")).one()[0] == "0001_initial"
