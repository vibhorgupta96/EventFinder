from __future__ import annotations

import pytest
from eventfinder.config import (
    OrganizerDefinition,
    OrganizersRegistry,
    SourceDefinition,
    SourcesRegistry,
    get_organizers_registry,
    get_sources_registry,
    validate_registry_relationships,
)
from pydantic import ValidationError

NEW_SOURCE_NAMES = (
    "foss_united_bengaluru",
    "global_ai_bengaluru",
    "cncf_bengaluru",
    "atlassian_bangalore",
    "open_source_india",
    "google_search_central",
    "google_developers",
    "salesforce_developer_events",
    "docker_events",
    "red_hat_events",
    "databricks_events",
)
DEFERRED_SOURCE_NAMES = {
    "open_source_india",
    "salesforce_developer_events",
    "docker_events",
    "red_hat_events",
}
ENABLED_NEW_SOURCE_NAMES = tuple(
    name for name in NEW_SOURCE_NAMES if name not in DEFERRED_SOURCE_NAMES
)


def test_builtin_source_registry_keeps_deferred_contracts_out_of_active_coverage():
    registry = get_sources_registry()
    assert len(registry.sources) == 23
    assert {source.name for source in registry.sources}.issuperset(NEW_SOURCE_NAMES)
    enabled_names = {source.name for source in registry.sources if source.enabled}
    assert len(enabled_names) == 19
    assert DEFERRED_SOURCE_NAMES.isdisjoint(enabled_names)
    assert set(ENABLED_NEW_SOURCE_NAMES).issubset(enabled_names)


@pytest.mark.parametrize("source_name", ENABLED_NEW_SOURCE_NAMES)
def test_each_new_source_has_a_deliberate_public_page_contract(source_name):
    source = next(source for source in get_sources_registry().sources if source.name == source_name)
    assert source.adapter == "public_page"
    assert source.url and source.allowed_domains
    assert source.max_pages == 1
    if source.max_detail_pages:
        assert source.detail_link_selectors or source.detail_link_prefixes
    else:
        assert not source.detail_link_selectors
        assert not source.detail_link_prefixes


def test_new_source_routes_and_external_provider_boundaries_are_explicit():
    sources = {source.name: source for source in get_sources_registry().sources}
    assert sources["foss_united_bengaluru"].url == "https://platform.fossunited.org/c/bengaluru"
    assert sources["foss_united_bengaluru"].detail_link_prefixes == ["/c/bengaluru/"]
    assert sources["global_ai_bengaluru"].detail_link_prefixes == [
        "https://globalai.community/e/"
    ]
    assert sources["cncf_bengaluru"].url == "https://community.cncf.io/cloud-native-bangalore/"
    assert sources["cncf_bengaluru"].detail_link_prefixes == ["/cncf/group/52r68y4/event/"]
    for name in ("google_search_central", "google_developers"):
        assert "rsvp.withgoogle.com" in sources[name].allowed_domains
        assert sources[name].detail_link_prefixes == ["https://rsvp.withgoogle.com/"]
    assert sources["databricks_events"].detail_link_prefixes == ["/resources/webinar/"]


def test_repaired_existing_calendars_have_bounded_official_detail_contracts():
    sources = {source.name: source for source in get_sources_registry().sources}
    assert sources["hasgeek"].url == "https://hasgeek.com/"
    assert sources["hasgeek"].detail_link_selectors == ["ul.upcoming a.card--upcoming[href]"]
    assert sources["hasgeek"].max_pages == 1
    assert sources["hasgeek"].pagination_param is None
    assert sources["hasgeek"].max_detail_pages == 4
    assert sources["gdg_bengaluru"].url == "https://gdg.community.dev/gdg-bangalore/"
    assert sources["gdg_bengaluru"].detail_link_prefixes == ["/events/details/google-gdg-bangalore-presents-"]
    assert sources["gdg_bengaluru"].max_detail_pages == 4
    assert sources["nvidia_developer"].url == "https://www.nvidia.com/content/dam/en-zz/Solutions/about-nvidia/webinar/webinarJSONData.json"
    assert sources["nvidia_developer"].platform == "nvidia_webinar"
    assert sources["nvidia_developer"].max_pages == 1
    assert sources["nvidia_developer"].max_detail_pages == 0


def test_new_source_registration_exceptions_are_narrow_and_explicit():
    sources = {source.name: source for source in get_sources_registry().sources}
    assert sources["google_search_central"].allowed_registration_domains == [
        "developers.google.com",
        "rsvp.withgoogle.com",
    ]
    assert sources["open_source_india"].allowed_registration_domains == [
        "opensourceindia.in"
    ]
    assert "ocgroups.dev" not in next(
        organizer for organizer in get_organizers_registry().organizers if organizer.name == "Cloud Native Computing Foundation"
    ).domains


def test_source_config_rejects_invalid_adapter_url_detail_and_duplicate_contracts():
    with pytest.raises(ValidationError, match="public_page sources require url"):
        SourceDefinition(name="missing_url", adapter="public_page")
    with pytest.raises(ValidationError, match="search sources require allowed_domains"):
        SourceDefinition(name="unbounded_search", adapter="search", query="events")
    with pytest.raises(ValidationError, match="only HTTP\\(S\\) URLs"):
        SourceDefinition(name="unsafe_url", adapter="public_page", url="file:///tmp/events")
    with pytest.raises(ValidationError, match="max_detail_pages requires"):
        SourceDefinition(
            name="unconfigured_details",
            adapter="public_page",
            url="https://events.example.test/",
            max_detail_pages=1,
        )
    source = SourceDefinition(name="duplicate", adapter="public_page", url="https://events.example.test/")
    with pytest.raises(ValidationError, match="source names must not contain duplicates"):
        SourcesRegistry(sources=[source, source])


def test_source_definition_rejects_an_unknown_iana_timezone():
    with pytest.raises(ValidationError, match="unknown IANA timezone"):
        SourceDefinition(
            name="bad_timezone",
            adapter="public_page",
            url="https://events.example.test/",
            source_timezone="Not/ARealZone",
        )


def test_source_definition_defaults_to_ist_and_dayfirst():
    source = SourceDefinition(name="default_tz", adapter="public_page", url="https://events.example.test/")
    assert source.source_timezone == "Asia/Kolkata"
    assert source.date_dayfirst is True


def test_cross_registry_source_profiles_must_reference_real_sources():
    sources = SourcesRegistry(
        sources=[SourceDefinition(name="known_source", adapter="public_page", url="https://events.example.test/")]
    )
    organizers = OrganizersRegistry(
        organizers=[OrganizerDefinition(name="Known", source_profiles=["missing_source"])]
    )
    with pytest.raises(ValueError, match="unknown sources: missing_source"):
        validate_registry_relationships(sources, organizers)
