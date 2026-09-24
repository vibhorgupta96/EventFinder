"""Database initialization and sessions. The SQLite file is project-local by default."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from eventfinder.config import get_settings


def _ensure_sqlite_parent(database_url: str) -> None:
    if database_url.startswith("sqlite:///"):
        sqlite_path = Path(database_url.removeprefix("sqlite:///"))
        if sqlite_path.name == ":memory:":
            return
        sqlite_path.parent.mkdir(parents=True, exist_ok=True)


def make_engine(database_url: str | None = None):
    url = database_url or get_settings().sqlalchemy_database_url
    _ensure_sqlite_parent(url)
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=connect_args)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            # Let writers wait up to 5s for a lock instead of failing
            # immediately under contention (e.g. concurrent discovery writes).
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    return engine


engine = make_engine()


def create_test_db_and_tables(target_engine=None) -> None:
    """Test-only schema helper; production schema changes go through Alembic."""
    SQLModel.metadata.create_all(target_engine or engine)


def get_session():
    with Session(engine) as session:
        yield session
