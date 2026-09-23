"""Transport objects independent of persistence and web frameworks."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


class EventFormat(StrEnum):
    IN_PERSON = "in_person"
    ONLINE = "online"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


class RegistrationState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    WAITLIST = "waitlist"
    SOLD_OUT = "sold_out"
    CANCELLED = "cancelled"
    POSTPONED = "postponed"
    UNKNOWN = "unknown"


class EventType(StrEnum):
    TALK = "talk"
    MEETUP = "meetup"
    WORKSHOP = "workshop"
    CONFERENCE = "conference"
    HACKATHON = "hackathon"
    BUILDATHON = "buildathon"
    COMPETITION = "competition"
    UNKNOWN = "unknown"


def has_explicit_paid_price(value: str | None) -> bool:
    """Recognize only affirmative price evidence; missing price remains unknown."""

    price = " ".join((value or "").casefold().split())
    if not price or any(word in price for word in ("free", "no cost", "nada")):
        return False
    currency = r"(?:₹|\$|€|£|inr|usd|rs\.?)"
    zero = rf"{currency}\s*0(?:\.0+)?(?:\s+(?:onwards|from))?"
    if re.fullmatch(zero, price) or re.fullmatch(r"0(?:\.0+)?", price):
        return False
    if re.search(r"\bpaid(?:\s+admission)?\b", price):
        return True
    if re.search(rf"{currency}\s*\d+(?:\.\d+)?", price):
        return True
    # The price field itself supplies the context for a bare price range or
    # an explicit "from/onwards" amount; do not infer payment from prose.
    return bool(
        re.fullmatch(
            r"(?:from\s+)?\d+(?:\.\d+)?(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?|\s+onwards)?",
            price,
        )
    )


class SourceEvidence(BaseModel):
    source_name: str
    source_url: str
    observed_at: datetime
    raw_id: str | None = None
    facts: dict[str, Any] = Field(default_factory=dict)


class EventCandidate(BaseModel):
    """A factual event candidate as observed on a public source."""

    title: str
    canonical_url: str
    source_url: str
    source_name: str
    organizer: str | None = None
    description: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    venue: str | None = None
    city: str | None = None
    country: str | None = None
    format: EventFormat = EventFormat.UNKNOWN
    event_type: EventType = EventType.UNKNOWN
    registration_state: RegistrationState = RegistrationState.UNKNOWN
    registration_url: str | None = None
    registration_deadline: datetime | None = None
    registration_opened_at: datetime | None = None
    # Observation metadata, never a claimed organizer-provided opening time.
    first_observed_open_at: datetime | None = None
    price_text: str | None = None
    is_explicitly_paid: bool = False
    eligibility_text: str | None = None
    speakers: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    evidence: SourceEvidence

    @field_validator("canonical_url", "source_url", "registration_url", mode="before")
    @classmethod
    def strip_urls(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator(
        "starts_at",
        "ends_at",
        "registration_deadline",
        "registration_opened_at",
        "first_observed_open_at",
    )
    @classmethod
    def normalize_datetimes(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class FetchResult(BaseModel):
    candidates: list[EventCandidate] = Field(default_factory=list)
    source_evidence: list[SourceEvidence] = Field(default_factory=list)


class AIClassification(BaseModel):
    is_technical: bool
    compatible_eligibility: bool
    event_type: EventType = EventType.UNKNOWN
    topics: list[str] = Field(default_factory=list)
    concise_summary: str = Field(max_length=400)
    rationale: str = Field(max_length=400)
    provider: str | None = None
    model: str | None = None
