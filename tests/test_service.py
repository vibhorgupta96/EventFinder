from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from eventfinder.config import SourceDefinition, SourcesRegistry
from eventfinder.domain import FetchResult, SourceEvidence
from eventfinder.models import Event, SourceRun
from eventfinder.service import DiscoveryService
from eventfinder.urls import URLSafety
from sqlmodel import Session, select


async def _public_resolver(_hostname: str) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.asyncio
async def test_candidate_failure_does_not_abort_source_run(session, candidate, config, organizers, monkeypatch):
    second = candidate.model_copy(deep=True)
    second.canonical_url = "https://events.example.test/second"
    second.title = "Bengaluru AI Platform Workshop"

    class FixtureSource:
        async def fetch(self):
            return FetchResult(candidates=[candidate, second])

    engine = session.get_bind()
    service = DiscoveryService(
        lambda: Session(engine),
        config,
        SourcesRegistry(sources=[]),
        organizers,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        safety=URLSafety(_public_resolver),
    )
    monkeypatch.setattr("eventfinder.service.make_source", lambda *_args, **_kwargs: FixtureSource())

    async def persist(_session, item):
        if item.title == candidate.title:
            raise ValueError("bad record")
        return "eligible"

    monkeypatch.setattr(service, "_persist_candidate", persist)
    result = await service._run_source(
        SourceDefinition(name="fixture", adapter="public_page", url="https://events.example.test/list")
    )
    with Session(engine) as verification:
        run = verification.exec(select(SourceRun)).one()
    await service.client.aclose()
    assert result == {"fetched": 2, "accepted": 1, "review": 0, "rejected": 0, "errors": 1}
    assert run.finished_at is not None
    assert run.error == "1 candidate errors"


@pytest.mark.asyncio
async def test_real_database_integrity_error_rolls_back_and_finalizes_source_run(
    session, candidate, config, organizers, monkeypatch
):
    second = candidate.model_copy(deep=True)
    second.canonical_url = "https://events.example.test/after-integrity-error"
    second.title = "Bengaluru AI Platform Workshop"

    class FixtureSource:
        async def fetch(self):
            return FetchResult(candidates=[candidate, second])

    engine = session.get_bind()
    service = DiscoveryService(
        lambda: Session(engine),
        config,
        SourcesRegistry(sources=[]),
        organizers,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        safety=URLSafety(_public_resolver),
    )
    monkeypatch.setattr("eventfinder.service.make_source", lambda *_args, **_kwargs: FixtureSource())

    async def persist(active_session, item):
        if item.title == candidate.title:
            active_session.add_all(
                [
                    Event(
                        canonical_url="https://events.example.test/duplicate",
                        normalized_key="duplicate-a",
                        title="Duplicate",
                    ),
                    Event(
                        canonical_url="https://events.example.test/duplicate",
                        normalized_key="duplicate-b",
                        title="Duplicate",
                    ),
                ]
            )
            active_session.commit()
        return "eligible"

    monkeypatch.setattr(service, "_persist_candidate", persist)
    result = await service._run_source(
        SourceDefinition(name="fixture", adapter="public_page", url="https://events.example.test/list")
    )
    with Session(engine) as verification:
        run = verification.exec(select(SourceRun)).one()
    await service.client.aclose()
    assert result == {"fetched": 2, "accepted": 1, "review": 0, "rejected": 0, "errors": 1}
    assert run.finished_at is not None
    assert run.error == "1 candidate errors"


@pytest.mark.asyncio
async def test_unexpected_source_error_is_recorded_and_later_sources_continue(
    session, candidate, config, organizers, monkeypatch
):
    class BrokenSource:
        async def fetch(self):
            raise RuntimeError("parser exploded")

    class GoodSource:
        async def fetch(self):
            return FetchResult(candidates=[candidate])

    engine = session.get_bind()
    service = DiscoveryService(
        lambda: Session(engine),
        config,
        SourcesRegistry(
            sources=[
                SourceDefinition(name="broken", adapter="public_page", url="https://events.example.test/broken"),
                SourceDefinition(name="good", adapter="public_page", url="https://events.example.test/good"),
            ]
        ),
        organizers,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        safety=URLSafety(_public_resolver),
    )
    monkeypatch.setattr(
        "eventfinder.service.make_source",
        lambda definition, *_args, **_kwargs: BrokenSource()
        if definition.name == "broken"
        else GoodSource(),
    )
    totals = await service.run_discovery(force=True)
    with Session(engine) as verification:
        runs = {
            run.source_name: run
            for run in verification.exec(select(SourceRun)).all()
        }
    await service.client.aclose()
    assert totals["sources"] == 2
    assert totals["accepted"] == 1
    assert totals["errors"] == 1
    assert runs["broken"].finished_at is not None
    assert runs["broken"].error == "parser exploded"
    assert runs["good"].finished_at is not None


@pytest.mark.asyncio
async def test_nonpriority_detail_rejection_is_recorded_and_later_sources_continue(
    session, candidate, config, organizers, monkeypatch
):
    class DetailRejectedSource:
        async def fetch(self):
            return FetchResult(
                source_evidence=[
                    SourceEvidence(
                        source_name="detail_rejected",
                        source_url="https://events.example.test/events/blocked",
                        observed_at=datetime.now(UTC),
                        facts={
                            "page_kind": "detail",
                            "rejected": "robots policy disallows this URL",
                        },
                    )
                ]
            )

    class GoodSource:
        async def fetch(self):
            return FetchResult(candidates=[candidate])

    engine = session.get_bind()
    service = DiscoveryService(
        lambda: Session(engine),
        config,
        SourcesRegistry(
            sources=[
                SourceDefinition(
                    name="detail_rejected",
                    adapter="public_page",
                    url="https://events.example.test/listing",
                ),
                SourceDefinition(
                    name="good_after_detail_rejection",
                    adapter="public_page",
                    url="https://events.example.test/good",
                ),
            ]
        ),
        organizers,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        safety=URLSafety(_public_resolver),
    )
    monkeypatch.setattr(
        "eventfinder.service.make_source",
        lambda definition, *_args, **_kwargs: DetailRejectedSource()
        if definition.name == "detail_rejected"
        else GoodSource(),
    )

    totals = await service.run_discovery(force=True)
    with Session(engine) as verification:
        runs = {
            run.source_name: run
            for run in verification.exec(select(SourceRun)).all()
        }
    await service.client.aclose()

    assert totals["accepted"] == 1
    assert totals["errors"] == 1
    assert runs["detail_rejected"].error == (
        "detail hydration rejected 1 page: 1 robots policy disallows this URL"
    )
    assert runs["good_after_detail_rejection"].error is None


@pytest.mark.asyncio
async def test_empty_nonpriority_calendar_without_detail_rejection_remains_successful(
    session, config, organizers, monkeypatch
):
    class EmptySource:
        async def fetch(self):
            return FetchResult()

    engine = session.get_bind()
    service = DiscoveryService(
        lambda: Session(engine),
        config,
        SourcesRegistry(sources=[]),
        organizers,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        safety=URLSafety(_public_resolver),
    )
    monkeypatch.setattr("eventfinder.service.make_source", lambda *_args, **_kwargs: EmptySource())

    result = await service._run_source(
        SourceDefinition(
            name="empty_calendar",
            adapter="public_page",
            url="https://events.example.test/empty",
        )
    )
    with Session(engine) as verification:
        run = verification.exec(select(SourceRun)).one()
    await service.client.aclose()

    assert result == {"fetched": 0, "accepted": 0, "review": 0, "rejected": 0, "errors": 0}
    assert run.error is None


@pytest.mark.asyncio
async def test_source_cancellation_remains_visible_to_the_scheduler(
    session, config, organizers, monkeypatch
):
    class CancelledSource:
        async def fetch(self):
            raise asyncio.CancelledError()

    engine = session.get_bind()
    service = DiscoveryService(
        lambda: Session(engine),
        config,
        SourcesRegistry(sources=[]),
        organizers,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200))),
        safety=URLSafety(_public_resolver),
    )
    monkeypatch.setattr("eventfinder.service.make_source", lambda *_args, **_kwargs: CancelledSource())
    try:
        with pytest.raises(asyncio.CancelledError):
            await service._run_source(
                SourceDefinition(name="cancelled", adapter="public_page", url="https://events.example.test/cancelled")
            )
    finally:
        await service.client.aclose()
