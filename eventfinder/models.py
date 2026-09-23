"""SQLModel persistence schema for EventFinder's independent SQLite database."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Column, String, UniqueConstraint
from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


class Event(SQLModel, table=True):
    __tablename__ = "events"

    id: int | None = Field(default=None, primary_key=True)
    canonical_url: str = Field(sa_column=Column(String, unique=True, index=True, nullable=False))
    normalized_key: str = Field(index=True)
    title: str
    organizer: str | None = Field(default=None, index=True)
    description: str | None = None
    concise_summary: str | None = None
    starts_at: datetime | None = Field(default=None, index=True)
    ends_at: datetime | None = None
    venue: str | None = None
    city: str | None = Field(default=None, index=True)
    country: str | None = None
    format: str = Field(default="unknown", index=True)
    event_type: str = Field(default="unknown", index=True)
    registration_state: str = Field(default="unknown", index=True)
    registration_url: str | None = None
    registration_deadline: datetime | None = None
    registration_opened_at: datetime | None = None
    first_observed_open_at: datetime | None = None
    price_text: str | None = None
    price_status: str = Field(default="not_stated")
    eligibility_text: str | None = None
    approval_required: bool = False
    speakers: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    topics: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    source_urls: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    relevance_reason: str | None = None
    organizer_trust: str = Field(default="low")
    ai_provenance: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    score: int = Field(default=0, index=True)
    status: str = Field(default="eligible", index=True)
    first_seen_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class EventSource(SQLModel, table=True):
    __tablename__ = "event_sources"
    __table_args__ = (UniqueConstraint("event_id", "source_url", name="uq_event_source_url"),)

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="events.id", index=True)
    source_name: str = Field(index=True)
    source_url: str
    raw_id: str | None = None
    evidence: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    observed_at: datetime = Field(default_factory=utcnow)


class EventChange(SQLModel, table=True):
    __tablename__ = "event_changes"

    id: int | None = Field(default=None, primary_key=True)
    event_id: int = Field(foreign_key="events.id", index=True)
    change_type: str = Field(index=True)
    old_value: str | None = None
    new_value: str | None = None
    observed_at: datetime = Field(default_factory=utcnow, index=True)
    digested_at: datetime | None = Field(default=None, index=True)


class SourceRun(SQLModel, table=True):
    __tablename__ = "source_runs"

    id: int | None = Field(default=None, primary_key=True)
    source_name: str = Field(index=True)
    started_at: datetime = Field(default_factory=utcnow, index=True)
    finished_at: datetime | None = None
    fetched_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    error: str | None = None
    status_code: int | None = None


class DigestRun(SQLModel, table=True):
    __tablename__ = "digest_runs"

    id: int | None = Field(default=None, primary_key=True)
    digest_date: str = Field(sa_column=Column(String, unique=True, index=True, nullable=False))
    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    status: str = Field(default="pending", index=True)
    event_change_ids: list[int] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))


class DigestDelivery(SQLModel, table=True):
    __tablename__ = "digest_deliveries"
    __table_args__ = (UniqueConstraint("digest_run_id", "chunk_index", name="uq_digest_delivery_chunk"),)

    id: int | None = Field(default=None, primary_key=True)
    digest_run_id: int = Field(foreign_key="digest_runs.id", index=True)
    chunk_index: int
    body: str
    telegram_message_id: str | None = None
    sent_at: datetime | None = None
    error: str | None = None
    attempt_count: int = 0
    next_attempt_at: datetime | None = Field(default=None, index=True)
