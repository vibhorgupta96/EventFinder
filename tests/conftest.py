from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from eventfinder.config import get_file_config, get_organizers_registry
from eventfinder.db import create_test_db_and_tables, make_engine
from eventfinder.domain import (
    EventCandidate,
    EventFormat,
    EventType,
    RegistrationState,
    SourceEvidence,
)
from sqlmodel import Session


@pytest.fixture
def session(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    create_test_db_and_tables(engine)
    with Session(engine) as session:
        yield session


@pytest.fixture
def candidate() -> EventCandidate:
    return EventCandidate(
        title="Bengaluru AI Systems Meetup",
        canonical_url="https://events.example.test/ai-systems",
        source_url="https://events.example.test/listing/ai-systems",
        source_name="gdg_bengaluru",
        organizer="Google Developer Groups Bengaluru",
        description="Technical AI engineering discussion and LLM workshop.",
        starts_at=datetime.now(UTC) + timedelta(days=8),
        city="Bengaluru",
        venue="Google Bengaluru",
        format=EventFormat.IN_PERSON,
        event_type=EventType.MEETUP,
        registration_state=RegistrationState.OPEN,
        speakers=["Ada Engineer"],
        topics=["AI"],
        evidence=SourceEvidence(
            source_name="gdg_bengaluru",
            source_url="https://events.example.test/listing/ai-systems",
            observed_at=datetime.now(UTC),
        ),
    )


@pytest.fixture
def config():
    return get_file_config()


@pytest.fixture
def organizers():
    return get_organizers_registry()
