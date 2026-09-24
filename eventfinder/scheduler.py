"""In-process scheduling for discovery, refresh, and the silent daily digest."""

from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from eventfinder.config import SchedulerConfig
from eventfinder.service import DiscoveryService
from eventfinder.telegram import DigestService


class EventFinderScheduler:
    def __init__(
        self, discovery: DiscoveryService, digest: DigestService, config: SchedulerConfig
    ) -> None:
        self.discovery = discovery
        self.digest = digest
        self.config = config
        self.scheduler = AsyncIOScheduler(timezone=config.timezone)
        # Guards discovery so a long-running run started by the scheduled job
        # or the startup refresh can never overlap the other.
        self._discovery_lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.scheduler.running

    async def discover_and_refresh(self) -> None:
        try:
            async with self._discovery_lock:
                await self.discovery.run_discovery()
                await self.discovery.refresh_known_events()
        except Exception:
            logger.exception("Scheduled discovery failed")

    async def deliver_digest(self) -> None:
        try:
            result = await self.digest.send_daily_digest()
            logger.info("Digest result: {}", result)
        except Exception:
            logger.exception("Scheduled digest failed")

    async def retry_digest(self) -> None:
        try:
            result = await self.digest.resume_or_catch_up(
                digest_hour_ist=self.config.digest_hour_ist
            )
            logger.info("Digest recovery result: {}", result)
        except Exception:
            logger.exception("Scheduled digest recovery failed")

    async def startup(self) -> None:
        # Startup deliberately performs one fresh read-only discovery rather
        # than waiting for the cadence recorded by a previous process.
        try:
            async with self._discovery_lock:
                await self.discovery.run_discovery(force=True)
                await self.discovery.refresh_known_events()
        except Exception:
            logger.exception("Startup discovery failed")
        await self.retry_digest()

    def start(self) -> None:
        if self.scheduler.running:
            return
        self.scheduler.add_job(
            self.discover_and_refresh,
            "interval",
            hours=self.config.discovery_hours,
            id="discovery",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self.retry_digest,
            "interval",
            minutes=15,
            id="digest_recovery",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.add_job(
            self.deliver_digest,
            CronTrigger(hour=self.config.digest_hour_ist, minute=0, timezone=self.config.timezone),
            id="daily_digest",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
