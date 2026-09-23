from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from eventfinder.config import get_settings
from eventfinder.db import _ensure_sqlite_parent
from eventfinder.models import SQLModel

config = context.config
# ``upgrade_database`` injects its target URL for test and deployment isolation.
# A direct ``alembic upgrade`` must still honor EventFinder's own settings rather
# than the placeholder URL committed in alembic.ini.
database_url = config.attributes.get("eventfinder_database_url") or get_settings().sqlalchemy_database_url
config.set_main_option("sqlalchemy.url", database_url)
_ensure_sqlite_parent(database_url)
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}), prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
