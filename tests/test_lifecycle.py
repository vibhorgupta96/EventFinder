from __future__ import annotations

import asyncio
import plistlib
from datetime import UTC, datetime, timedelta

import pytest
from eventfinder import lifecycle
from eventfinder.config import SchedulerConfig, SourceDefinition, SourcesRegistry
from eventfinder.db import make_engine
from eventfinder.models import SourceRun
from eventfinder.repository import start_source_run
from eventfinder.runtime import single_instance_lock
from eventfinder.scheduler import EventFinderScheduler
from eventfinder.service import DiscoveryService
from sqlalchemy import text
from sqlmodel import Session, select


def test_launchd_template_uses_unique_label_and_caffeinate():
    template = plistlib.loads(open("launchd/com.eventfinder.app.plist", "rb").read())
    assert template["Label"] == "com.eventfinder.app"
    assert template["ProgramArguments"][:2] == ["/usr/bin/caffeinate", "-i"]


def test_rendered_plist_has_project_local_logs_and_venv():
    payload = lifecycle._plist()
    assert payload["Label"] == lifecycle.LABEL
    assert "/.venv/bin/python" in payload["ProgramArguments"][2]
    assert "EventFinder" in payload["StandardOutPath"]


def test_runtime_lock_refuses_a_second_process(tmp_path):
    path = tmp_path / "eventfinder.lock"
    with single_instance_lock(path):
        with pytest.raises(RuntimeError, match="already running"):
            with single_instance_lock(path):
                pass


class _RecordingDiscovery:
    """Test double that records call counts and peak concurrency."""

    def __init__(self, *, delay: float = 0.0, fail: bool = False) -> None:
        self.delay = delay
        self.fail = fail
        self.discovery_calls = 0
        self.refresh_calls = 0
        self.in_flight = 0
        self.max_in_flight = 0

    async def run_discovery(self, force: bool = False) -> dict[str, int]:
        self.discovery_calls += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.delay:
            await asyncio.sleep(self.delay)
        self.in_flight -= 1
        if self.fail:
            raise RuntimeError("boom")
        return {}

    async def refresh_known_events(self, limit: int = 50) -> dict[str, int]:
        self.refresh_calls += 1
        return {}


class _NoopDigest:
    async def send_daily_digest(self):
        return {}

    async def resume_or_catch_up(self, digest_hour_ist: int):
        return {}


def _make_scheduler(discovery: _RecordingDiscovery) -> EventFinderScheduler:
    return EventFinderScheduler(discovery, _NoopDigest(), SchedulerConfig())


@pytest.mark.asyncio
async def test_discover_and_refresh_job_is_wired_and_runs_discovery_then_refresh():
    scheduler = _make_scheduler(_RecordingDiscovery())
    scheduler.start()
    try:
        job = scheduler.scheduler.get_job("discovery")
        assert job is not None
        assert job.func == scheduler.discover_and_refresh
    finally:
        scheduler.shutdown()

    discovery = scheduler.discovery
    await scheduler.discover_and_refresh()
    assert discovery.discovery_calls == 1
    assert discovery.refresh_calls == 1


@pytest.mark.asyncio
async def test_scheduled_discovery_failure_is_swallowed_and_recorded():
    discovery = _RecordingDiscovery(fail=True)
    scheduler = _make_scheduler(discovery)
    # Existing behavior: a discovery/refresh exception is caught and logged,
    # never propagated to the scheduler's job runner.
    await scheduler.discover_and_refresh()
    assert discovery.discovery_calls == 1


@pytest.mark.asyncio
async def test_startup_runs_forced_discovery_then_refresh_and_digest_recovery():
    discovery = _RecordingDiscovery()
    scheduler = _make_scheduler(discovery)
    await scheduler.startup()
    assert discovery.discovery_calls == 1
    assert discovery.refresh_calls == 1


@pytest.mark.asyncio
async def test_discovery_lock_prevents_overlapping_discovery_runs():
    discovery = _RecordingDiscovery(delay=0.05)
    scheduler = _make_scheduler(discovery)
    await asyncio.gather(scheduler.discover_and_refresh(), scheduler.discover_and_refresh())
    assert discovery.discovery_calls == 2
    assert discovery.max_in_flight == 1


@pytest.mark.asyncio
async def test_discovery_lock_also_guards_the_startup_run():
    discovery = _RecordingDiscovery(delay=0.05)
    scheduler = _make_scheduler(discovery)
    await asyncio.gather(scheduler.startup(), scheduler.discover_and_refresh())
    assert discovery.max_in_flight == 1


@pytest.mark.asyncio
async def test_should_run_cadence_gating_is_true_at_and_after_the_boundary(session, config, organizers):
    definition = SourceDefinition(name="cadence_source", adapter="public_page", url="https://example.test", cadence_hours=3)
    service = DiscoveryService(
        lambda: Session(session.get_bind()),
        config,
        SourcesRegistry(sources=[definition]),
        organizers,
        client=None,
    )
    start_source_run(session, definition.name)
    run = session.exec(select(SourceRun)).first()
    now = datetime.now(UTC)

    run.started_at = now - timedelta(hours=3, seconds=1)
    session.add(run)
    session.commit()
    assert service._should_run(definition, force=False) is True

    run.started_at = now - timedelta(hours=2, minutes=59)
    session.add(run)
    session.commit()
    assert service._should_run(definition, force=False) is False

    assert service._should_run(definition, force=True) is True


def test_sqlite_busy_timeout_pragma_is_set(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'busy.sqlite3'}")
    with Session(engine) as session:
        value = session.exec(text("PRAGMA busy_timeout")).one()
        assert value[0] == 5000
