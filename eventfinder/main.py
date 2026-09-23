"""Application entry point. Run through the isolated EventFinder environment."""

from __future__ import annotations

import uvicorn
from loguru import logger

from eventfinder.config import get_file_config
from eventfinder.migrations import upgrade_database
from eventfinder.runtime import single_instance_lock
from eventfinder.web import create_app


def run() -> None:
    config = get_file_config()
    logger.info("Starting EventFinder on {}:{}", config.server.host, config.server.port)
    with single_instance_lock():
        upgrade_database()
        uvicorn.run(create_app(), host=config.server.host, port=config.server.port, log_level="info")


if __name__ == "__main__":
    run()
