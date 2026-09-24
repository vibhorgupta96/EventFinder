from __future__ import annotations

import asyncio

import pytest
from eventfinder.config import SourceDefinition, SourcesRegistry
from eventfinder.domain import FetchResult
from eventfinder.live_smoke import build_parser, run_live_smoke


@pytest.mark.asyncio
async def test_live_smoke_bounds_sources_times_out_and_uses_temporary_database(
    config, organizers, monkeypatch
):
    called: list[str] = []

    class SlowSource:
        async def fetch(self):
            called.append("slow")
            await asyncio.sleep(60)

    class EmptySource:
        async def fetch(self):
            called.append("empty")
            return FetchResult()

    def source_factory(definition, *_args, **_kwargs):
        if definition.name == "slow":
            return SlowSource()
        if definition.name == "empty":
            return EmptySource()
        raise AssertionError(f"unselected source was constructed: {definition.name}")

    monkeypatch.setattr("eventfinder.service.make_source", source_factory)
    emitted: list[str] = []
    summary = await run_live_smoke(
        max_sources=2,
        timeout=0.01,
        config=config,
        sources=SourcesRegistry(
            sources=[
                SourceDefinition(name="slow", adapter="public_page", url="https://events.example.test/slow"),
                SourceDefinition(name="empty", adapter="public_page", url="https://events.example.test/empty"),
                SourceDefinition(name="unselected", adapter="public_page", url="https://events.example.test/unselected"),
                SourceDefinition(name="disabled", adapter="public_page", url="https://events.example.test/disabled", enabled=False),
            ]
        ),
        organizers=organizers,
        emit=emitted.append,
    )
    assert called == ["slow", "empty"]
    assert len(summary.observations) == 2
    assert summary.observations[0].error == "read-only smoke timeout after 0.01s"
    assert summary.observations[1].fetched == 0
    assert summary.database_url.endswith("live-smoke.sqlite3")
    assert "eventfinder-live-smoke-" in summary.database_url
    assert any(line.startswith("summary:") for line in emitted)


def test_live_smoke_parser_accepts_repeatable_targeted_sources():
    args = build_parser().parse_args(
        ["--max-sources", "2", "--source", "google_developers", "--source", "databricks_events"]
    )
    assert args.max_sources == 2
    assert args.source_names == ["google_developers", "databricks_events"]


@pytest.mark.asyncio
async def test_live_smoke_selects_requested_enabled_source_only(config, organizers, monkeypatch):
    called: list[str] = []

    class EmptySource:
        async def fetch(self):
            return FetchResult()

    def source_factory(definition, *_args, **_kwargs):
        called.append(definition.name)
        return EmptySource()

    monkeypatch.setattr("eventfinder.service.make_source", source_factory)
    sources = SourcesRegistry(
        sources=[
            SourceDefinition(name="first", adapter="public_page", url="https://events.example.test/first"),
            SourceDefinition(name="target", adapter="public_page", url="https://events.example.test/target"),
            SourceDefinition(name="disabled", adapter="public_page", url="https://events.example.test/disabled", enabled=False),
        ]
    )
    summary = await run_live_smoke(
        max_sources=1,
        timeout=1,
        source_names=["target"],
        config=config,
        sources=sources,
        organizers=organizers,
        emit=lambda _line: None,
    )
    assert called == ["target"]
    assert [observation.source_name for observation in summary.observations] == ["target"]


@pytest.mark.asyncio
async def test_live_smoke_rejects_unknown_duplicate_or_over_bound_targets(config, organizers):
    sources = SourcesRegistry(
        sources=[
            SourceDefinition(name="first", adapter="public_page", url="https://events.example.test/first"),
            SourceDefinition(name="second", adapter="public_page", url="https://events.example.test/second"),
        ]
    )
    for source_names, message in (
        (["missing"], "unknown or disabled"),
        (["first", "first"], "must not be repeated"),
        (["first", "second"], "exceed max_sources"),
    ):
        with pytest.raises(ValueError, match=message):
            await run_live_smoke(
                max_sources=1,
                timeout=1,
                source_names=source_names,
                config=config,
                sources=sources,
                organizers=organizers,
            )
