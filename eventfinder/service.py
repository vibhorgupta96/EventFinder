"""Discovery orchestration: fetch public sources, apply policy, persist provenance."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from loguru import logger
from sqlmodel import Session, select

from eventfinder.ai import AIClassifier
from eventfinder.config import FileConfig, OrganizersRegistry, SourceDefinition, SourcesRegistry
from eventfinder.domain import EventCandidate
from eventfinder.models import Event, SourceRun
from eventfinder.policy import assess_candidate
from eventfinder.repository import (
    expire_past_events,
    find_existing,
    finish_source_run,
    start_source_run,
    upsert_candidate,
)
from eventfinder.sources import RequestLimiter, RobotsPolicy, SourceFetchError, make_source
from eventfinder.urls import URLSafety


def _detail_hydration_error(result) -> tuple[str | None, int]:
    """Summarize bounded detail-page failures without failing other sources."""

    reasons = [
        str(evidence.facts["rejected"])
        for evidence in result.source_evidence
        if evidence.facts.get("page_kind") == "detail" and evidence.facts.get("rejected")
    ]
    if not reasons:
        return None, 0
    counts = Counter(reasons)
    rendered = "; ".join(f"{count} {reason}" for reason, count in sorted(counts.items()))
    pages = "page" if len(reasons) == 1 else "pages"
    return f"detail hydration rejected {len(reasons)} {pages}: {rendered}", len(reasons)


class DiscoveryService:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        config: FileConfig,
        sources: SourcesRegistry,
        organizers: OrganizersRegistry,
        client: httpx.AsyncClient,
        classifier: AIClassifier | None = None,
        safety: URLSafety | None = None,
        limiter: RequestLimiter | None = None,
    ):
        self.session_factory = session_factory
        self.config = config
        self.sources = sources
        self.organizers = organizers
        self.client = client
        self.classifier = classifier
        self.safety = safety or URLSafety()
        self.robots_policy = RobotsPolicy(client, self.safety)
        self.limiter = limiter or RequestLimiter()

    def _should_run(self, definition: SourceDefinition, force: bool) -> bool:
        if force:
            return True
        with self.session_factory() as session:
            latest = session.exec(
                select(SourceRun)
                .where(SourceRun.source_name == definition.name)
                .order_by(SourceRun.started_at.desc())
            ).first()
            if latest is None:
                return True
            started_at = latest.started_at
            started_at = started_at.replace(tzinfo=UTC) if started_at.tzinfo is None else started_at.astimezone(UTC)
            elapsed = datetime.now(UTC) - started_at
            return elapsed.total_seconds() >= definition.cadence_hours * 3600

    async def run_discovery(self, force: bool = False) -> dict[str, int]:
        """Discover sources independently: one bad page never stops the rest."""

        totals = {"sources": 0, "fetched": 0, "accepted": 0, "review": 0, "rejected": 0, "errors": 0}
        with self.session_factory() as session:
            totals["expired"] = expire_past_events(session)
        for definition in self.sources.sources:
            if not definition.enabled or not self._should_run(definition, force):
                continue
            totals["sources"] += 1
            result = await self._run_source(definition)
            for key, value in result.items():
                totals[key] = totals.get(key, 0) + value
        logger.info("Discovery complete: {}", totals)
        return totals

    async def _run_source(self, definition: SourceDefinition) -> dict[str, int]:
        with self.session_factory() as session:
            run = start_source_run(session, definition.name)
            try:
                source = make_source(
                    definition,
                    self.client,
                    self.robots_policy,
                    self.safety,
                    self.limiter,
                )
                result = await source.fetch()
            except SourceFetchError as error:
                finish_source_run(session, run, error=str(error), status_code=error.status_code)
                logger.warning("Source {} unavailable: {}", definition.name, error)
                return {"errors": 1}
            except (httpx.HTTPError, ValueError) as error:
                finish_source_run(session, run, error=str(error))
                logger.warning("Source {} failed: {}", definition.name, error)
                return {"errors": 1}
            except Exception as error:
                # Deliberately catch only Exception: cancellation, exit, and
                # keyboard interrupts remain supervisor-visible control flow.
                finish_source_run(session, run, error=str(error))
                logger.exception("Source {} failed unexpectedly: {}", definition.name, error)
                return {"errors": 1}

            accepted = review = rejected = candidate_errors = 0
            for candidate in result.candidates:
                try:
                    outcome = await self._persist_candidate(session, candidate)
                except Exception as error:
                    session.rollback()
                    candidate_errors += 1
                    logger.exception("Candidate from {} failed persistence: {}", definition.name, error)
                    continue
                if outcome == "eligible":
                    accepted += 1
                elif outcome == "needs_review":
                    review += 1
                else:
                    rejected += 1
            detail_error, detail_errors = _detail_hydration_error(result)
            zero_parse_error = (
                "priority source returned zero candidates or interstitial page"
                if definition.priority and not result.candidates and not detail_error
                else None
            )
            run_error = "; ".join(
                part
                for part in (
                    detail_error,
                    zero_parse_error,
                    f"{candidate_errors} candidate errors" if candidate_errors else None,
                )
                if part
            ) or None
            finish_source_run(
                session,
                run,
                fetched_count=len(result.candidates),
                accepted_count=accepted,
                rejected_count=rejected,
                error=run_error,
            )
            return {
                "fetched": len(result.candidates),
                "accepted": accepted,
                "review": review,
                "rejected": rejected,
                "errors": candidate_errors + detail_errors,
            }

    async def _persist_candidate(self, session: Session, candidate: EventCandidate) -> str:
        existing = find_existing(session, candidate)
        if (
            existing
            and existing.registration_state in {"unknown", "closed"}
            and candidate.registration_state.value == "open"
            and candidate.first_observed_open_at is None
        ):
            candidate.first_observed_open_at = candidate.evidence.observed_at
        assessment = await assess_candidate(
            candidate,
            self.config,
            self.organizers,
            classifier=self.classifier,
        )
        upsert_candidate(session, candidate, assessment)
        return assessment.status

    async def refresh_known_events(self, limit: int = 50) -> dict[str, int]:
        """Refresh factual registration/status fields from known public event pages."""

        now = datetime.now(UTC)
        with self.session_factory() as session:
            events = list(
                session.exec(
                    select(Event)
                    .where(Event.status == "eligible")
                    .order_by(Event.starts_at)
                    .limit(limit)
                ).all()
            )
        refreshed = errors = 0
        for event in events:
            visibility_end = event.ends_at or event.starts_at
            if visibility_end and (
                visibility_end.replace(tzinfo=UTC) if visibility_end.tzinfo is None else visibility_end.astimezone(UTC)
            ) < now:
                continue
            definition = SourceDefinition(
                name="known_event_refresh",
                adapter="public_page",
                url=event.canonical_url,
                enabled=True,
                cadence_hours=3,
                rate_limit_seconds=1,
            )
            try:
                result = await make_source(
                    definition,
                    self.client,
                    self.robots_policy,
                    self.safety,
                    self.limiter,
                ).fetch()
            except (SourceFetchError, httpx.HTTPError, ValueError) as error:
                logger.info("Could not refresh {}: {}", event.canonical_url, error)
                errors += 1
                continue
            with self.session_factory() as session:
                for candidate in result.candidates:
                    await self._persist_candidate(session, candidate)
            refreshed += 1
        return {"refreshed": refreshed, "errors": errors}
