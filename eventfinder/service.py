"""Discovery orchestration: fetch public sources, apply policy, persist provenance."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import httpx
from loguru import logger
from sqlmodel import Session, select

from eventfinder.ai import AIClassifier
from eventfinder.config import FileConfig, OrganizersRegistry, SourceDefinition, SourcesRegistry
from eventfinder.domain import EventCandidate
from eventfinder.models import Event, EventSource, SourceRun
from eventfinder.policy import assess_candidate
from eventfinder.repository import (
    expire_past_events,
    find_existing,
    finish_source_run,
    revalidate_notification_policy,
    start_source_run,
    upsert_candidate,
)
from eventfinder.sources import RequestLimiter, RobotsPolicy, SourceFetchError, make_source
from eventfinder.urls import (
    UnsafeURL,
    URLSafety,
    nvidia_webinar_identity_url,
    same_event_destination,
    validate_url_syntax,
)


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


def _source_domains(definition: SourceDefinition, *, registration: bool = False) -> set[str]:
    hostname = urlsplit(definition.url).hostname if definition.url else None
    if registration:
        owned = {hostname.casefold()} if hostname else set(definition.allowed_domains)
        return owned | set(definition.allowed_registration_domains)
    return set(definition.allowed_domains) | ({hostname.casefold()} if hostname else set())


def _within_source(url: str, domains: set[str]) -> bool:
    try:
        hostname = urlsplit(validate_url_syntax(url)).hostname or ""
    except UnsafeURL:
        return False
    return any(hostname.casefold() == domain or hostname.casefold().endswith(f".{domain}") for domain in domains)


def _observed_event_provenance(source: EventSource, definition: SourceDefinition, canonical_url: str) -> bool:
    parser = source.evidence.get("parser")
    if source.evidence.get("rejected"):
        return False
    if definition.platform == "nvidia_webinar" or parser == "nvidia_webinar_feed":
        portal_identity = nvidia_webinar_identity_url(canonical_url)
        portal = urlsplit(portal_identity) if portal_identity else None
        webinar_id = portal.fragment.removeprefix("/webinar/") if portal else None
        observed_event = source.evidence.get("event")
        return (
            parser == "nvidia_webinar_feed"
            and definition.platform == "nvidia_webinar"
            and definition.url == "https://www.nvidia.com/content/dam/en-zz/Solutions/about-nvidia/webinar/webinarJSONData.json"
            and source.source_url == definition.url
            and portal is not None
            and not portal.query
            and webinar_id is not None
            and int(webinar_id) > 0
            and source.raw_id == webinar_id
            and isinstance(observed_event, dict)
            and str(observed_event.get("eventId")) == webinar_id
        )
    return isinstance(parser, str) and (
        parser in {"json_ld", "embedded_json", "opengraph", "meetup:apollo", "ocg:attendance"}
        or parser.endswith((":semantic_labels", ":html"))
    )


def _refresh_origin(event: Event, sources: list[EventSource],
                    definitions: dict[str, SourceDefinition]) -> tuple[EventSource, SourceDefinition] | None:
    attributed = sorted(
        sources,
        key=lambda source: ({"low": 0, "medium": 1, "high": 2}.get(source.evidence.get("organizer_trust"), 0), source.observed_at),
        reverse=True,
    )
    return next((
        (source, definitions[source.source_name])
        for source in attributed
        if source.source_name in definitions
        and _observed_event_provenance(source, definitions[source.source_name], event.canonical_url)
        and _within_source(source.source_url, _source_domains(definitions[source.source_name]))
        and _within_source(event.canonical_url, _source_domains(definitions[source.source_name]))
    ), None)


def _review_refresh_at(sources: list[EventSource]) -> datetime | None:
    attempts = []
    for source in sources:
        value = source.evidence.get("admission_review_refresh_at")
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        attempts.append(parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC))
    return max(attempts) if attempts else None


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
            revalidate_notification_policy(session, self.config)
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
        upsert_candidate(session, candidate, assessment, self.config)
        return assessment.status

    async def refresh_known_events(self, limit: int = 50) -> dict[str, int]:
        """Refresh factual registration/status fields from known public event pages."""

        now = datetime.now(UTC)
        limit = max(0, min(limit, 50))
        if not limit:
            return {"refreshed": 0, "errors": 0, "skipped": 0}
        definitions = {source.name: source for source in self.sources.sources if source.enabled}
        with self.session_factory() as session:
            eligible = list(
                session.exec(
                    select(Event)
                    .where(Event.status == "eligible")
                    .order_by(Event.starts_at)
                    .limit(limit)
                ).all()
            )
            review = list(session.exec(select(Event).where(
                Event.status == "needs_review",
                Event.relevance_reason.in_([
                    "Free admission is not verified",
                    "Legacy free admission lacks event-specific evidence",
                ]),
                Event.starts_at >= now,
                Event.starts_at <= now + timedelta(days=self.config.policy.newly_opened_extended_days),
            )).all())
            provenance = {
                event.id: list(session.exec(select(EventSource).where(EventSource.event_id == event.id)).all())
                for event in [*eligible, *review]
            }
        origins = {event.id: _refresh_origin(event, provenance[event.id], definitions)
                   for event in [*eligible, *review]}
        # Filter cooling rows before applying the cap. Oldest attempts go
        # first so a repeatedly unavailable source cannot starve other rows.
        due = []
        for event in review:
            origin = origins[event.id]
            if origin is None:
                continue
            attempted_at = _review_refresh_at(provenance[event.id])
            if attempted_at and now - attempted_at < timedelta(hours=origin[1].cadence_hours):
                continue
            due.append((attempted_at or datetime.min.replace(tzinfo=UTC), event.id, event))
        review_budget = min(10, max(1, limit // 5))
        if eligible:
            review_budget = min(review_budget, limit - 1)
        recovering = [item[2] for item in sorted(due, key=lambda item: item[:2])[:review_budget]]
        recovery_ids = {event.id for event in recovering}
        events = [*eligible[:limit - len(recovering)], *recovering]
        refreshed = errors = skipped = 0
        for event in events:
            visibility_end = event.ends_at or event.starts_at
            if visibility_end and (
                visibility_end.replace(tzinfo=UTC) if visibility_end.tzinfo is None else visibility_end.astimezone(UTC)
            ) < now:
                continue
            origin = origins[event.id]
            if origin is None:
                logger.info("Skipping refresh without verified source provenance: {}", event.canonical_url)
                skipped += 1
                continue
            attributed_source, original = origin
            if event.id in recovery_ids:
                # This is an operational cooldown, never an admission fact.
                # Save attempts before fetching so denial/error paths respect
                # the same source cadence as successful refreshes.
                with self.session_factory() as session:
                    source_row = session.get(EventSource, attributed_source.id)
                    if source_row is not None:
                        source_row.evidence = {**source_row.evidence,
                                               "admission_review_refresh_at": now.isoformat()}
                        session.add(source_row)
                        session.commit()
            # Preserve source identity, parsing conventions and boundaries;
            # refreshing one event must not crawl its related-event links.
            # NVIDIA's verified HashRouter identities exist only in its feed;
            # refetch that configured feed and filter to this event below.
            definition = SourceDefinition.model_validate({
                **original.model_dump(),
                "adapter": "public_page",
                "url": original.url if original.platform == "nvidia_webinar" else event.canonical_url,
                "query": None,
                "allowed_domains": sorted(_source_domains(original)),
                "allowed_registration_domains": sorted(_source_domains(original, registration=True)),
                "max_pages": 1,
                "pagination_param": None,
                "detail_link_prefixes": [],
                "detail_link_selectors": [],
                "max_detail_pages": 0,
            })
            try:
                source = make_source(
                    definition,
                    self.client,
                    self.robots_policy,
                    self.safety,
                    self.limiter,
                )
                # A permitted redirect host is not automatically a permitted
                # registration host merely because it is the refreshed URL.
                source.allowed_registration_domains = _source_domains(original, registration=True)
                result = await source.fetch()
            except (SourceFetchError, httpx.HTTPError, ValueError) as error:
                logger.info("Could not refresh {}: {}", event.canonical_url, error)
                errors += 1
                continue
            matching = [candidate for candidate in result.candidates if same_event_destination(candidate.canonical_url, event.canonical_url)]
            if not matching:
                skipped += 1
                continue
            with self.session_factory() as session:
                persisted = False
                for candidate in matching:
                    existing = find_existing(session, candidate)
                    if existing is not None and existing.id == event.id:
                        await self._persist_candidate(session, candidate)
                        persisted = True
            if persisted:
                refreshed += 1
            else:
                skipped += 1
        return {"refreshed": refreshed, "errors": errors, "skipped": skipped}
