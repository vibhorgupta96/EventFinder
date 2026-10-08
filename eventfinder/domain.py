"""Transport objects independent of persistence and web frameworks."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Any
from urllib.parse import urljoin

from pydantic import BaseModel, Field, field_validator

from eventfinder.urls import UnsafeURL, normalize_url, same_event_destination


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


MEETUP_NO_FEE_TEXT = "No Meetup fee"


def normalize_price(value: Any) -> str | None:
    """Normalize only finite, explicitly supplied price scalar values."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not isfinite(value):
            return None
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, str):
        return " ".join(value.split()) or None
    return None


def has_explicit_paid_price(value: Any) -> bool:
    """Recognize only affirmative price evidence; missing price remains unknown."""

    normalized = normalize_price(value)
    price = normalized.casefold() if normalized else ""
    if not price:
        return False
    if has_required_admission_charge(price):
        return True
    if ";" in price and any(has_explicit_paid_price(part) for part in price.split(";")):
        return True
    currency = r"(?:₹|\$|€|£|(?<![a-z])(?:inr|usd|eur|cad|aud|gbp|sgd|jpy|cny)(?![a-z])|\brs\.?)"
    number = r"(\d+(?:\.\d+)?)"
    amounts = re.findall(rf"(?:{currency}\s*{number}|{number}\s*{currency})", price)
    numeric_amounts = [next(amount for amount in match if amount) for match in amounts]
    if any(float(amount) > 0 for amount in numeric_amounts):
        return True
    if re.search(r"\bpaid(?:\s+admission)?\b", price):
        return True
    if re.search(r"\b(?:buy|purchase|purchased|purchasing)\s+(?:an?\s+)?(?:tickets?|passes?)\b|"
                 r"\b(?:tickets?|passes?)\s+(?:must be |need to be )?(?:purchased|bought)\b", price):
        return True
    if any(word in price for word in ("free", "no cost", "nada")):
        return False
    if re.fullmatch(rf"{currency}\s*0(?:\.0+)?(?:\s+(?:onwards|from))?", price):
        return False
    if re.fullmatch(r"0(?:\.0+)?", price):
        return False
    # The price field itself supplies the context for a bare price range or
    # an explicit "from/onwards" amount; do not infer payment from prose.
    return bool(
        re.fullmatch(
            r"(?:from\s+)?\d+(?:\.\d+)?(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?|\s+onwards)?",
            price,
        )
    )


# A bare number (no currency) after admission/entry/registration/ticket/pass is
# not a charge when it reads as a clock time, numeric date or day-month date (a
# day or hour has at most two digits), or a duration/headcount word follows it:
# "registration 3 days before", "registration 10:30 am", "tickets 7 oct",
# "tickets: 50 seats". Any money signal in the whole clause (a currency token or
# a fee/payment word) keeps the paid verdict.
_NON_MONEY_NUMBER = (
    r"\d{1,2}:\d{2}(?!\d)|\d{4}-\d{1,2}-\d{1,2}(?!\d)|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}(?!\d)"
    r"|\d{1,2}(?:\.\d{2})?\s*(?:-\s*)?[ap]\.?m\b"
    r"|\d{1,2}\s*(?:-\s*)?(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may(?=\s*(?:\d|,|\)|$))"
    r"|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b"
    r"|\d+(?:,\d{3})*(?:\.\d+)?\s*(?:-\s*)?(?:hrs?|hours?|mins?|minutes?|days?|weeks?|months?|years?"
    r"|seats?|spots?|slots?|attendees?|participants?)\b"
)
_STATED_MONEY = r"₹|\$|€|£|/-|(?<![a-z])(?:inr|usd|eur|cad|aud|gbp|sgd|rs|rupees?|dollars?|euros?)(?![a-z])"
_PAYMENT_WORD = (
    r"\b(?:pa(?:id|ys?|ying|yable|yments?)|fees?|costs?|pric(?:e|es|ed|ing)|charg(?:e|es|ed|ing)|chargeable"
    r"|contributions?|deposits?|purchas(?:e|es|ed|ing)|amounts?|buy(?:ing)?)\b"
)


def _bare_number_is_not_money(clause_has_money: bool, text: str, found: re.Match[str] | None) -> bool:
    """True for a currency-less time/date/count in a clause with no money signal."""

    return bool(
        found
        and not clause_has_money
        and re.fullmatch(r"\d+(?:\.\d+)?\s*", found.group(1))
        and re.match(_NON_MONEY_NUMBER, text[found.start(1):])
    )


def has_required_admission_charge(value: str | None) -> bool:
    """Find charges for attendance in event-local prose, not arbitrary money."""

    text = (value or "").casefold()
    currency = r"(?:₹|\$|€|£|inr|usd|eur|cad|aud|gbp|sgd|rs\.?)"
    amount = rf"(?:{currency}\s*\d+(?:\.\d+)?|\d+(?:\.\d+)?\s*(?:{currency})?)"
    # "Rs." ends in a period but not a sentence: keep "Rs. 200" in one clause.
    for clause in re.split(r"[;\n]|(?<=[.!?])(?<!\brs\.)\s+", text):
        clause_has_money = bool(re.search(_STATED_MONEY, clause) or re.search(_PAYMENT_WORD, clause))
        for donation in re.finditer(r"\bdonations?\b", clause):
            before, after = clause[:donation.start()], clause[donation.end():]
            if (re.search(r"\b(?:optional|voluntary)\s+(?:[\w-]+\s+){0,2}$", before)
                    or re.match(r"\s*(?:(?:is|are)\s+)?(?:optional|voluntary)\b", after)
                    or re.search(r"\bnot\s+(?:required|mandatory)\b", after)):
                continue
            stated = re.match(rf"\s*(?:(?:of|is|:)\s*)?({amount})(?!\w)", after)
            following = after[stated.end():] if stated else after
            required_after = re.match(r"\s*(?:(?:is|are)\s+)?(?:required|mandatory)\b|"
                                      r"\s*must\s+be\s+(?:paid|made|given)\b", following)
            required = required_after or re.search(
                r"\b(?:mandatory|required)\s+$|"
                r"\b(?:must|have to|need to)\s+(?:make|pay|give)\s+(?:an?\s+)?$", before
            )
            attendance = re.search(r"\b(?:to|for)\s+(?:attend(?:ance|ees)?|enter|entry|participate|register|registration|admission)\b", clause)
            if required and attendance:
                if stated is None and required_after:
                    stated = re.search(rf"(?::|\b(?:of|at))\s*({amount})(?!\w)",
                                       following[required_after.end():])
                if stated is None or has_explicit_paid_price(stated.group(1)):
                    return True
        for match in re.finditer(
            r"\b(?:admission|entry|registration|tickets?|passes?)(?:\s+(?:fees?|charges?|costs?|prices?))?\b",
            clause,
        ):
            before, after = clause[:match.start()], clause[match.end():].strip()
            # Negated attendance fees and optional exam/credential costs are
            # compatible with a free technical event.
            if re.search(r"\b(?:no|without)\s+(?:an?\s+)?(?:paid\s+)?$", before):
                continue
            if re.search(r"\b(?:certification|exam)\s+(?:[\w-]+\s+){0,2}$", before):
                continue
            if re.match(r"(?:(?:is|are|does|do)\s+)?(?:not\b|waived\b|free\b)", after):
                continue
            if re.match(r"(?:(?:is|are)\s+)?paid\b", after):
                return True
            required = re.match(r"(?:(?:is|are)\s+)?(?:required|appl(?:y|ies)|payable|mandatory)\b", after)
            if required:
                stated = re.match(rf"\s*(?::|of|at)?\s*({amount})(?!\w)", after[required.end():])
                if stated and not _bare_number_is_not_money(clause_has_money, after[required.end():], stated):
                    if has_explicit_paid_price(stated.group(1)):
                        return True
                    continue  # An explicitly required zero fee is still free.
                if re.search(r"\b(?:fees?|charges?|costs?|prices?)\b", match.group(0)):
                    return True
                # A required reservation/free ticket is not a money charge.
            if re.search(r"\bpaid\s+$", before):
                return True
            if re.match(r"(?:must be|need to be|have to be)\s+(?:purchased|bought)\b", after):
                return True
            price = re.match(rf"(?:(?:of|is|are|costs?|from|priced at)\s+|:\s*)?({amount})(?!\w)", after)
            if (price and has_explicit_paid_price(price.group(1))
                    and not _bare_number_is_not_money(clause_has_money, after, price)):
                return True
    return False


def observation_facts(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Read primary and nested merge provenance with bounded traversal."""

    pending, result = [facts], []
    while pending and len(result) < 128:
        current = pending.pop()
        result.append(current)
        observations = current.get("merged_observations", [])
        if not isinstance(observations, list):
            continue
        pending.extend(item["facts"] for item in observations[:128]
                       if isinstance(item, dict) and isinstance(item.get("facts"), dict))
    return result


def free_admission_statement(value: str | None) -> str | None:
    """Extract an affirmative event-subject statement, retaining its exact wording."""

    # Descriptions can contain numbered notes with their newlines flattened.
    # Require a complete statement: free Wi-Fi, conditional offers, and free
    # attendance only for a subset of visitors are not admission evidence.
    match = re.search(
        r"(?:^|[\n;.!?]\s*)(?:\d+[.)]\s*)?"
        r"(?P<statement>(?:(?:the|this)\s+"
        r"(?:event|workshop|meetup|talk|session|conference|hackathon|buildathon)|attendance)\s+is\s+free"
        r"(?:\s+of\s+(?:cost|charge|cos)|\s+to\s+attend)?)"
        r"(?=$|[\n;.!]|\s+\d+[.)](?:\s|$))",
        value or "",
        re.I,
    )
    return match.group("statement") if match else None


def admission_price_status(value: Any, explicitly_paid: bool = False) -> str:
    """Require admission-specific evidence, rather than an incidental 'free'."""

    if explicitly_paid or has_explicit_paid_price(value):
        return "paid"
    price = (normalize_price(value) or "").casefold()
    if "?" in price or "？" in price:
        return "not_stated"
    if re.search(r"\b(?:not|isn't|is not|no)\s+free\b", price):
        return "not_stated"
    if ";" in price and any(admission_price_status(part) == "free" for part in price.split(";")):
        return "free"
    if price == MEETUP_NO_FEE_TEXT.casefold():
        return "free"
    currency = r"(?:₹|\$|€|£|inr|usd|eur|cad|aud|gbp|sgd|jpy|cny|rs\.?)"
    if re.fullmatch(rf"(?:{currency}\s*)?0(?:\.0+)?(?:\s*{currency})?", price):
        return "free"
    if re.fullmatch(r"(?:free(?: of (?:cost|charge)| to attend)?|no cost|complimentary)[.!]?", price):
        return "free"
    if free_admission_statement(price):
        return "free"
    if re.search(
        r"\b(?:free (?:admission|entry|tickets?)|"
        r"(?:admission|entry|tickets?) (?:is |are )?(?:free|at no cost)|"
        r"no (?:admission|entry) fee)\b", price
    ):
        return "free"
    return "not_stated"


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
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        # Defensive dedupe normalization only: a non-http(s) value or one
        # that otherwise fails URL safety is returned untouched so the
        # dedicated validators/``safety.validate`` still run and can reject
        # it with the real reason, never silently swallowed here.
        try:
            return normalize_url(stripped)
        except UnsafeURL:
            return stripped

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


def event_facts_match(candidate: EventCandidate, facts: dict[str, Any]) -> bool:
    """Admission facts with an event URL must identify this same event."""

    node = facts.get("event")
    if not isinstance(node, dict):
        return True
    event_url = next((node.get(key) for key in ("url", "eventUrl", "event_url", "permalink", "link")
                      if node.get(key)), None)
    if event_url is None:
        return True
    return isinstance(event_url, str) and same_event_destination(
        urljoin(candidate.source_url, event_url), candidate.canonical_url
    )


def _admission_descriptions(candidate: EventCandidate) -> list[str]:
    descriptions = [candidate.description] if candidate.description and event_facts_match(
        candidate, candidate.evidence.facts
    ) else []
    for facts in observation_facts(candidate.evidence.facts):
        if not event_facts_match(candidate, facts):
            continue
        if isinstance(statement := facts.get("admission_statement"), str):
            descriptions.append(statement)
        node = facts.get("event")
        if isinstance(node, dict):
            descriptions.extend(value for key in ("description", "summary", "about", "blurb")
                                if isinstance(value := node.get(key), str))
    return descriptions


def candidate_admission_statement(candidate: EventCandidate) -> str | None:
    return next((statement for description in _admission_descriptions(candidate)
                 if (statement := free_admission_statement(description))), None)


def candidate_admission_status(candidate: EventCandidate) -> str:
    """Apply the same event-local admission facts during assessment and delivery."""

    descriptions = _admission_descriptions(candidate)
    status = admission_price_status(candidate.price_text, candidate.is_explicitly_paid)
    if status == "paid" or any(has_required_admission_charge(text) for text in descriptions):
        return "paid"
    if any(re.search(r"\b(?:(?:the|this)\s+(?:event|workshop|meetup|talk|session|conference|hackathon|buildathon)|attendance)"
                     r"\s+(?:is\s+(?:not|never)|isn['’]t)\s+(?:a\s+)?free\b|"
                     r"\b(?:not|isn['’]t)\s+(?:a\s+)?free\s+event\b",
                     text, re.I) for text in descriptions):
        return "not_stated"
    if status == "free" or candidate_admission_statement(candidate):
        return "free"
    return "not_stated"


_PAYMENT_AMOUNT = (
    r"(?:(?:₹|\$|\brs\.?|\binr|\busd)\s*\d|\d\s*(?:₹|\$|\brs\b\.?|\binr\b|\busd\b|/-|rupees?\b))"
)
_PAYMENT_TERMS = (
    r"\b(?:pa(?:id|ys?|ying|yable|yments?)|fees?|costs?|pric(?:e|es|ed|ing)|charg(?:e|es|ed|ing)"
    r"|contributions?|ticket(?:s|ed|ing)?|rupees?|donations?|chargeable|pass(?:es)?|deposits?"
    r"|purchas(?:e|es|ed|ing)|amounts?)\b"
    r"|\b(?:entry|admission|registration|participation)\s+(?:is\s+)?not\s+free\b"
)


def mentions_payment_terms(text: str | None) -> bool:
    """Conservatively detect any currency amount or payment wording."""
    return bool(re.search(f"{_PAYMENT_AMOUNT}|{_PAYMENT_TERMS}", text or "", re.I))


def free_only_from_meetup_fee_settings(candidate: EventCandidate) -> bool:
    """True when free admission rests solely on Meetup's empty fee setting."""
    if candidate_admission_status(candidate) != "free":
        return False
    parts = [part.strip() for part in (candidate.price_text or "").split(";")]
    if not any(part.casefold() == MEETUP_NO_FEE_TEXT.casefold() for part in parts):
        return False
    remaining = "; ".join(p for p in parts if p and p.casefold() != MEETUP_NO_FEE_TEXT.casefold()) or None
    return candidate_admission_status(candidate.model_copy(update={"price_text": remaining})) != "free"


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
