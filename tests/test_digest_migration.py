"""Digest delivery attribution upgrades leave older saved bodies intact."""

from alembic import command
from alembic.config import Config
from eventfinder.config import PROJECT_ROOT
from sqlalchemy import create_engine, text


def _config(path):
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.attributes["eventfinder_database_url"] = f"sqlite:///{path}"
    return config


def test_existing_digest_delivery_upgrades_with_unknown_mapping(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    config = _config(path)
    command.upgrade(config, "0001_initial")
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO digest_runs (id, digest_date, started_at, status, event_change_ids)
            VALUES (1, '2026-09-23', '2026-09-23 03:00:00', 'partial', '[]')
        """))
        connection.execute(text("""
            INSERT INTO digest_deliveries
                (id, digest_run_id, chunk_index, body, attempt_count)
            VALUES (1, 1, 0, 'saved body', 1)
        """))
    command.upgrade(config, "head")
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT body, attempt_count, event_change_ids FROM digest_deliveries WHERE id = 1"
        )).one()
        assert row == ("saved body", 1, None)
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            "0002_digest_delivery_change_ids"
        )


def test_fresh_database_has_delivery_mapping_column(tmp_path):
    path = tmp_path / "fresh.sqlite3"
    command.upgrade(_config(path), "head")
    engine = create_engine(f"sqlite:///{path}")
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(digest_deliveries)"))}
    assert "event_change_ids" in columns
