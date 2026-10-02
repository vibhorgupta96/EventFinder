"""Bounded, read-only smoke collection against a small public-source sample."""

from __future__ import annotations

import argparse
import asyncio
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from eventfinder.config import (
    FileConfig,
    OrganizersRegistry,
    SourcesRegistry,
    get_file_config,
    get_organizers_registry,
    get_sources_registry,
)
from eventfinder.db import make_engine
from eventfinder.migrations import upgrade_database
from eventfinder.models import SourceRun
from eventfinder.repository import finish_source_run
from eventfinder.service import DiscoveryService
from eventfinder.urls import make_public_fetch_client


@dataclass(frozen=True)
class SourceObservation:
    source_name: str
    status: str
    fetched: int
    accepted: int
    rejected: int
    error: str | None
    status_code: int | None


@dataclass(frozen=True)
class LiveSmokeSummary:
    database_url: str
    observations: list[SourceObservation]

    @property
    def totals(self) -> dict[str, int]:
        return {
            "sources": len(self.observations),
            "fetched": sum(item.fetched for item in self.observations),
            "accepted": sum(item.accepted for item in self.observations),
            "rejected": sum(item.rejected for item in self.observations),
            "observed_errors": sum(item.error is not None for item in self.observations),
        }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least one")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _latest_observation(session_factory: Callable[[], Session], source_name: str) -> SourceObservation:
    with session_factory() as session:
        run = session.exec(
            select(SourceRun)
            .where(SourceRun.source_name == source_name)
            .order_by(SourceRun.started_at.desc())
        ).first()
    if run is None:
        # This is an orchestration defect, not a source outcome: every
        # selected source must at least produce a durable SourceRun.
        raise RuntimeError(f"no source run recorded for {source_name}")
    return SourceObservation(
        source_name=source_name,
        status="ok" if run.error is None else "observed_error",
        fetched=run.fetched_count,
        accepted=run.accepted_count,
        rejected=run.rejected_count,
        error=run.error,
        status_code=run.status_code,
    )


def _record_timeout(
    session_factory: Callable[[], Session], source_name: str, timeout_seconds: float
) -> None:
    with session_factory() as session:
        run = session.exec(
            select(SourceRun)
            .where(SourceRun.source_name == source_name)
            .order_by(SourceRun.started_at.desc())
        ).first()
        if run is None:
            raise RuntimeError(f"timed out before source run started: {source_name}")
        if run.finished_at is None:
            finish_source_run(
                session,
                run,
                error=f"read-only smoke timeout after {timeout_seconds:g}s",
            )


async def run_live_smoke(
    *,
    max_sources: int = 2,
    timeout: float = 30,
    source_names: list[str] | None = None,
    config: FileConfig | None = None,
    sources: SourcesRegistry | None = None,
    organizers: OrganizersRegistry | None = None,
    emit: Callable[[str], None] = print,
) -> LiveSmokeSummary:
    """Read a bounded set of public sources without touching production data.

    Source/network outcomes remain observations: robots denial, CAPTCHA/403,
    429, timeouts, or zero parsed candidates are emitted and returned rather
    than made into process failures. Configuration, schema, and local
    orchestration errors intentionally propagate to the CLI.
    """

    if max_sources < 1:
        raise ValueError("max_sources must be at least one")
    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")
    config = config or get_file_config()
    sources = sources or get_sources_registry()
    organizers = organizers or get_organizers_registry()
    enabled_by_name = {source.name: source for source in sources.sources if source.enabled}
    if source_names:
        if len(source_names) != len(set(source_names)):
            raise ValueError("source names must not be repeated")
        unknown = [name for name in source_names if name not in enabled_by_name]
        if unknown:
            raise ValueError("unknown or disabled source: " + ", ".join(unknown))
        if len(source_names) > max_sources:
            raise ValueError("selected sources exceed max_sources")
        selected = [enabled_by_name[name] for name in source_names]
    else:
        selected = [source for source in sources.sources if source.enabled][:max_sources]

    with tempfile.TemporaryDirectory(prefix="eventfinder-live-smoke-") as directory:
        database_url = f"sqlite:///{Path(directory) / 'live-smoke.sqlite3'}"
        # Production Alembic schema is exercised against this disposable file.
        upgrade_database(database_url)
        engine = make_engine(database_url)

        def session_factory() -> Session:
            return Session(engine)

        observations: list[SourceObservation] = []
        async with make_public_fetch_client(timeout=timeout) as client:
            discovery = DiscoveryService(
                session_factory,
                config,
                SourcesRegistry(sources=selected),
                organizers,
                client,
            )
            for source in selected:
                try:
                    await asyncio.wait_for(discovery._run_source(source), timeout=timeout)
                except TimeoutError:
                    _record_timeout(session_factory, source.name, timeout)
                observation = _latest_observation(session_factory, source.name)
                observations.append(observation)
                emit(
                    f"{observation.source_name}: status={observation.status} "
                    f"fetched={observation.fetched} "
                    f"accepted={observation.accepted} rejected={observation.rejected} "
                    f"status_code={observation.status_code or '-'} "
                    f"error={observation.error or '-'}"
                )
        summary = LiveSmokeSummary(database_url=database_url, observations=observations)
        emit("summary: " + " ".join(f"{key}={value}" for key, value in summary.totals.items()))
        return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read a bounded sample of EventFinder public sources into a temporary database."
    )
    parser.add_argument("--max-sources", type=_positive_int, default=2)
    parser.add_argument("--timeout", type=_positive_float, default=30)
    parser.add_argument(
        "--source",
        dest="source_names",
        action="append",
        metavar="NAME",
        help="run a named enabled source (repeatable; remains bounded by --max-sources)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        asyncio.run(
            run_live_smoke(
                max_sources=args.max_sources,
                timeout=args.timeout,
                source_names=args.source_names,
            )
        )
    except (OSError, RuntimeError, ValueError) as error:
        print(f"EventFinder live smoke failed locally: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
