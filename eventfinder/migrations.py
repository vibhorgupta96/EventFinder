"""Alembic is the production schema authority."""

from __future__ import annotations

from alembic import command
from alembic.config import Config

from eventfinder.config import PROJECT_ROOT, get_settings
from eventfinder.db import _ensure_sqlite_parent


def upgrade_database(database_url: str | None = None) -> None:
    url = database_url or get_settings().sqlalchemy_database_url
    _ensure_sqlite_parent(url)
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["eventfinder_database_url"] = url
    command.upgrade(config, "head")
