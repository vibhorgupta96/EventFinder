"""initial eventfinder schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("canonical_url", sa.String(), nullable=False, unique=True),
        sa.Column("normalized_key", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("organizer", sa.String()),
        sa.Column("description", sa.String()),
        sa.Column("concise_summary", sa.String()),
        sa.Column("starts_at", sa.DateTime(timezone=True)),
        sa.Column("ends_at", sa.DateTime(timezone=True)),
        sa.Column("venue", sa.String()),
        sa.Column("city", sa.String()),
        sa.Column("country", sa.String()),
        sa.Column("format", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("registration_state", sa.String(), nullable=False),
        sa.Column("registration_url", sa.String()),
        sa.Column("registration_deadline", sa.DateTime(timezone=True)),
        sa.Column("registration_opened_at", sa.DateTime(timezone=True)),
        sa.Column("first_observed_open_at", sa.DateTime(timezone=True)),
        sa.Column("price_text", sa.String()),
        sa.Column("price_status", sa.String(), nullable=False),
        sa.Column("eligibility_text", sa.String()),
        sa.Column("approval_required", sa.Boolean(), nullable=False),
        sa.Column("speakers", sa.JSON(), nullable=False),
        sa.Column("topics", sa.JSON(), nullable=False),
        sa.Column("source_urls", sa.JSON(), nullable=False),
        sa.Column("relevance_reason", sa.String()),
        sa.Column("organizer_trust", sa.String(), nullable=False),
        sa.Column("ai_provenance", sa.JSON(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("canonical_url", "normalized_key", "organizer", "starts_at", "city", "format", "event_type", "registration_state", "score", "status", "last_seen_at"):
        op.create_index(f"ix_events_{column}", "events", [column])
    op.create_table(
        "event_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("source_name", sa.String(), nullable=False),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("raw_id", sa.String()),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", "source_url", name="uq_event_source_url"),
    )
    op.create_index("ix_event_sources_event_id", "event_sources", ["event_id"])
    op.create_index("ix_event_sources_source_name", "event_sources", ["source_name"])
    op.create_table(
        "event_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("change_type", sa.String(), nullable=False),
        sa.Column("old_value", sa.String()),
        sa.Column("new_value", sa.String()),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("digested_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_event_changes_event_id", "event_changes", ["event_id"])
    op.create_index("ix_event_changes_change_type", "event_changes", ["change_type"])
    op.create_index("ix_event_changes_observed_at", "event_changes", ["observed_at"])
    op.create_index("ix_event_changes_digested_at", "event_changes", ["digested_at"])
    op.create_table(
        "source_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_name", sa.String(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("fetched_count", sa.Integer(), nullable=False),
        sa.Column("accepted_count", sa.Integer(), nullable=False),
        sa.Column("rejected_count", sa.Integer(), nullable=False),
        sa.Column("error", sa.String()),
        sa.Column("status_code", sa.Integer()),
    )
    op.create_index("ix_source_runs_source_name", "source_runs", ["source_name"])
    op.create_index("ix_source_runs_started_at", "source_runs", ["started_at"])
    op.create_table(
        "digest_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("digest_date", sa.String(), nullable=False, unique=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("event_change_ids", sa.JSON(), nullable=False),
    )
    op.create_index("ix_digest_runs_digest_date", "digest_runs", ["digest_date"])
    op.create_index("ix_digest_runs_status", "digest_runs", ["status"])
    op.create_table(
        "digest_deliveries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("digest_run_id", sa.Integer(), sa.ForeignKey("digest_runs.id"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("body", sa.String(), nullable=False),
        sa.Column("telegram_message_id", sa.String()),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.String()),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("digest_run_id", "chunk_index", name="uq_digest_delivery_chunk"),
    )
    op.create_index("ix_digest_deliveries_digest_run_id", "digest_deliveries", ["digest_run_id"])
    op.create_index("ix_digest_deliveries_next_attempt_at", "digest_deliveries", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_table("digest_deliveries")
    op.drop_table("digest_runs")
    op.drop_table("source_runs")
    op.drop_table("event_changes")
    op.drop_table("event_sources")
    op.drop_table("events")
