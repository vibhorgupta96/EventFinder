from __future__ import annotations

import pytest
from eventfinder.migrations import upgrade_database
from eventfinder.policy import assess_candidate
from eventfinder.repository import upsert_candidate
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
        health = client.get("/healthz")
        assert dashboard.status_code == 200
        assert "Bengaluru AI Systems Meetup" in dashboard.text
        assert response.status_code == 200
        assert response.json()["events"][0]["title"] == candidate.title
        assert health.status_code == 200
        assert client.post("/api/events").status_code == 405


def test_clean_temp_database_is_created_by_alembic_only(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'nested' / 'migrated.sqlite3'}"
    upgrade_database(database_url)
    app = create_app(database_url=database_url, start_scheduler=False)
    with Session(app.state.engine) as session:
        assert session.exec(text("SELECT version_num FROM alembic_version")).one()[0] == "0001_initial"
