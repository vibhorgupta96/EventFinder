"""Bounded offline smoke check: parser -> policy -> SQLite -> dashboard model."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import Session

from eventfinder.config import get_file_config, get_organizers_registry
from eventfinder.db import create_test_db_and_tables, make_engine
from eventfinder.domain import (
    EventCandidate,
    EventFormat,
    EventType,
    RegistrationState,
    SourceEvidence,
)
from eventfinder.policy import assess_candidate
from eventfinder.repository import list_events, upsert_candidate


async def _run() -> None:
    with tempfile.TemporaryDirectory() as directory:
        engine = make_engine(f"sqlite:///{Path(directory) / 'smoke.sqlite3'}")
        create_test_db_and_tables(engine)
        candidate = EventCandidate(
            title="Bengaluru AI Systems Meetup",
            canonical_url="https://events.example.test/ai-systems",
            source_url="https://events.example.test/ai-systems",
            source_name="smoke",
            organizer="Google Developer Groups Bengaluru",
            description="Technical AI engineering talk and hands-on session.",
            starts_at=datetime.now(UTC) + timedelta(days=7),
            city="Bengaluru",
            format=EventFormat.IN_PERSON,
            event_type=EventType.MEETUP,
            registration_state=RegistrationState.OPEN,
            evidence=SourceEvidence(
                source_name="smoke",
                source_url="https://events.example.test/ai-systems",
                observed_at=datetime.now(UTC),
            ),
        )
        assessment = await assess_candidate(candidate, get_file_config(), get_organizers_registry())
        assert assessment.status == "eligible", assessment
        with Session(engine) as session:
            upsert_candidate(session, candidate, assessment)
            assert len(list_events(session)) == 1
    print("EventFinder smoke check passed")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
