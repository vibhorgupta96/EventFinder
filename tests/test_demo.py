from __future__ import annotations

from datetime import UTC, datetime

import pytest
from eventfinder.config import Settings
from eventfinder.demo import OfflineSettings, temporary_demo
from fastapi.testclient import TestClient
from pydantic_settings.sources import DotEnvSettingsSource, EnvSettingsSource

DEMO_NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def test_default_database_engine_attribute_remains_lazy_and_available(monkeypatch):
    from eventfinder import db

    marker = object()
    monkeypatch.setattr(db, "_default_engine", lambda: marker)
    assert db.engine is marker


def test_reviewer_demo_ignores_environment_and_dotenv_credentials(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n".join(
            (
                "EVENTFINDER_GEMINI_API_KEY=dotenv-gemini-secret",
                "EVENTFINDER_GROQ_API_KEY=dotenv-groq-secret",
                "EVENTFINDER_TELEGRAM_BOT_TOKEN=dotenv-telegram-secret",
                "EVENTFINDER_TELEGRAM_CHAT_ID=dotenv-chat-secret",
                f"EVENTFINDER_DATABASE_URL=sqlite:///{tmp_path / 'dotenv.sqlite3'}",
            )
        ),
        encoding="utf-8",
    )
    isolated_model_config = {**Settings.model_config, "env_file": dotenv}
    monkeypatch.setattr(Settings, "model_config", isolated_model_config)
    monkeypatch.setattr(OfflineSettings, "model_config", isolated_model_config)
    monkeypatch.setenv("EVENTFINDER_GEMINI_API_KEY", "inherited-gemini-secret")
    monkeypatch.setenv("EVENTFINDER_GROQ_API_KEY", "inherited-groq-secret")
    monkeypatch.setenv("EVENTFINDER_TELEGRAM_BOT_TOKEN", "inherited-telegram-secret")
    monkeypatch.setenv("EVENTFINDER_TELEGRAM_CHAT_ID", "inherited-chat-secret")
    monkeypatch.setenv("EVENTFINDER_DATABASE_URL", f"sqlite:///{tmp_path / 'environment.sqlite3'}")

    def forbidden_settings_source(*args, **kwargs):
        pytest.fail("the offline demo must not construct an environment or dotenv source")

    monkeypatch.setattr(EnvSettingsSource, "__init__", forbidden_settings_source)
    monkeypatch.setattr(DotEnvSettingsSource, "__init__", forbidden_settings_source)
    monkeypatch.setattr(
        DotEnvSettingsSource,
        "_read_env_files",
        forbidden_settings_source,
    )

    def forbidden_settings_access():
        pytest.fail("the demo must use its injected settings, not environment or dotenv settings")

    monkeypatch.setattr("eventfinder.web.get_settings", forbidden_settings_access)
    monkeypatch.setattr("eventfinder.web.get_file_config", forbidden_settings_access)
    monkeypatch.setattr("eventfinder.web.get_sources_registry", forbidden_settings_access)
    monkeypatch.setattr("eventfinder.web.get_organizers_registry", forbidden_settings_access)

    with temporary_demo(now=DEMO_NOW) as app:
        assert app.state.settings.gemini_api_key is None
        assert app.state.settings.groq_api_key is None
        assert app.state.settings.telegram_bot_token is None
        assert app.state.settings.telegram_chat_id is None
        assert app.state.discovery.classifier.classifiers == []
        assert app.state.digest.sender is None
        assert app.state.sources_registry.sources == []
        assert app.state.demo_transports[0].requests == []
        assert app.state.demo_transports[1].requests == []
        assert not (tmp_path / "dotenv.sqlite3").exists()
        assert not (tmp_path / "environment.sqlite3").exists()


def test_reviewer_demo_serves_labeled_synthetic_rows_and_date_filters(tmp_path, monkeypatch):
    monkeypatch.setattr("eventfinder.repository.utcnow", lambda: DEMO_NOW)
    with temporary_demo(now=DEMO_NOW) as app:
        demo_root = app.state.demo_root
        database_path = app.state.demo_database_path
        assert database_path.exists()
        assert app.state.demo_host == "127.0.0.1"
        assert app.state.demo_port == 18766

        with TestClient(app) as client:
            dashboard = client.get("/")
            all_events = client.get("/api/events")
            one_day = client.get(
                "/api/events",
                params={"start_after": "2026-10-10", "start_before": "2026-10-10"},
            )

            assert dashboard.status_code == 200
            assert "DEMO DATA — Synthetic examples only" in dashboard.text
            assert "DEMO · Testing accessible forms" in dashboard.text
            assert "DEMO · Practical AI evaluation" in dashboard.text
            assert all_events.status_code == 200
            assert len(all_events.json()["events"]) == 3
            assert all(event["title"].startswith("DEMO ·") for event in all_events.json()["events"])
            assert one_day.status_code == 200
            assert [event["title"] for event in one_day.json()["events"]] == [
                "DEMO · Testing accessible forms"
            ]

            assert app.state.scheduler.running is False
            assert app.state.scheduler.scheduler.get_jobs() == []

        assert app.state.scheduler.running is False
        assert all(client.is_closed for client in app.state.demo_clients)
        assert all(transport.requests == [] for transport in app.state.demo_transports)

    assert not database_path.exists()
    assert not demo_root.exists()
