"""Typed, project-local configuration and source registries."""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from eventfinder.urls import UnsafeURL, validate_url_syntax

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SOURCE_NAME_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"
_DOMAIN_PATTERN = r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"


def _validated_url(value: str) -> str:
    try:
        return validate_url_syntax(value)
    except UnsafeURL as error:
        raise ValueError(str(error)) from error


def _validated_domain(value: str) -> str:
    domain = value.strip().rstrip(".").casefold()
    if not re.fullmatch(_DOMAIN_PATTERN, domain):
        raise ValueError("must be a public DNS domain without a scheme or path")
    return domain


def _unique_values(values: list[str], label: str) -> list[str]:
    if len(values) != len(set(values)):
        raise ValueError(f"{label} must not contain duplicates")
    return values


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8766


class SchedulerConfig(BaseModel):
    discovery_hours: int = Field(default=3, ge=1, le=24)
    digest_hour_ist: int = Field(default=9, ge=0, le=23)
    timezone: str = "Asia/Kolkata"


class PolicyConfig(BaseModel):
    near_future_days: int = Field(default=60, ge=1)
    newly_opened_extended_days: int = Field(default=180, ge=60)
    registration_opened_days: int = Field(default=7, ge=1)
    dashboard_limit: int = Field(default=100, ge=1, le=500)


class AIConfig(BaseModel):
    provider_order: list[Literal["gemini", "groq"]] = ["gemini", "groq"]
    timeout_seconds: int = Field(default=15, ge=1, le=60)


class RankingConfig(BaseModel):
    minimum_score: int = 3
    digest_limit: int = Field(default=10, ge=1, le=10)


class FileConfig(BaseModel):
    server: ServerConfig = ServerConfig()
    scheduler: SchedulerConfig = SchedulerConfig()
    policy: PolicyConfig = PolicyConfig()
    ai: AIConfig = AIConfig()
    ranking: RankingConfig = RankingConfig()
    topics: dict[str, list[str]] = {"include": [], "exclude": []}


class SourceDefinition(BaseModel):
    name: str = Field(pattern=_SOURCE_NAME_PATTERN)
    adapter: Literal["public_page", "search"]
    url: str | None = None
    query: str | None = None
    enabled: bool = True
    priority: bool = False
    cadence_hours: int = Field(default=3, ge=1, le=24)
    rate_limit_seconds: float = Field(default=3, ge=0)
    platform: str | None = None
    max_pages: int = Field(default=1, ge=1, le=5)
    pagination_param: str | None = None
    profile_url: str | None = None
    allowed_domains: list[str] = Field(default_factory=list)
    # Detail hydration is deliberately opt-in and bounded. Prefixes are
    # absolute public URLs or same-origin paths; selectors identify public
    # anchors/cards on the configured listing only.
    detail_link_prefixes: list[str] = Field(default_factory=list, max_length=8)
    detail_link_selectors: list[str] = Field(default_factory=list, max_length=8)
    max_detail_pages: int = Field(default=0, ge=0, le=10)
    # Registration links are display-only, but still get a boundary separate
    # from the page-fetch redirect boundary. An empty list defaults at runtime
    # to the source's own allowed domains.
    allowed_registration_domains: list[str] = Field(default_factory=list)

    @field_validator("url", "profile_url")
    @classmethod
    def validate_public_urls(cls, value: str | None) -> str | None:
        return _validated_url(value) if value is not None else None

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("allowed_domains", "allowed_registration_domains")
    @classmethod
    def validate_domains(cls, values: list[str]) -> list[str]:
        return _unique_values([_validated_domain(value) for value in values], "domains")

    @field_validator("detail_link_prefixes")
    @classmethod
    def validate_detail_prefixes(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            prefix = value.strip()
            if prefix.startswith("/"):
                if prefix.startswith("//"):
                    raise ValueError("detail link paths must not be protocol-relative")
            else:
                prefix = _validated_url(prefix)
            normalized.append(prefix)
        return _unique_values(normalized, "detail link prefixes")

    @field_validator("detail_link_selectors")
    @classmethod
    def validate_detail_selectors(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value or len(value) > 200 for value in normalized):
            raise ValueError("detail link selectors must be non-empty CSS selectors under 200 characters")
        return _unique_values(normalized, "detail link selectors")

    @model_validator(mode="after")
    def validate_adapter_contract(self) -> SourceDefinition:
        if self.adapter == "public_page":
            if not self.url:
                raise ValueError("public_page sources require url")
            if self.query:
                raise ValueError("public_page sources must not define query")
        else:
            if not self.query:
                raise ValueError("search sources require query")
            if self.url:
                raise ValueError("search sources must not define url")
            if not self.allowed_domains:
                raise ValueError("search sources require allowed_domains")
        if self.max_pages > 1 and not self.pagination_param:
            raise ValueError("max_pages above one requires pagination_param")
        configured_detail_rules = bool(self.detail_link_prefixes or self.detail_link_selectors)
        if self.adapter != "public_page" and configured_detail_rules:
            raise ValueError("only public_page sources may configure detail hydration")
        if self.max_detail_pages and not configured_detail_rules:
            raise ValueError("max_detail_pages requires a detail link prefix or selector")
        if configured_detail_rules and not self.max_detail_pages:
            raise ValueError("detail link rules require max_detail_pages")
        return self


class SourcesRegistry(BaseModel):
    sources: list[SourceDefinition]

    @model_validator(mode="after")
    def validate_unique_source_names(self) -> SourcesRegistry:
        _unique_values([source.name for source in self.sources], "source names")
        return self


class OrganizerDefinition(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    trust: Literal["high", "medium", "low"] = "low"
    online_allowed: bool = False
    source_profiles: list[str] = Field(default_factory=list)

    @field_validator("domains")
    @classmethod
    def validate_organizer_domains(cls, values: list[str]) -> list[str]:
        return _unique_values([_validated_domain(value) for value in values], "organizer domains")

    @field_validator("source_profiles")
    @classmethod
    def validate_source_profiles(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not re.fullmatch(_SOURCE_NAME_PATTERN, value) for value in normalized):
            raise ValueError("source profiles must be valid source names")
        return _unique_values(normalized, "source profiles")


class OrganizersRegistry(BaseModel):
    organizers: list[OrganizerDefinition]

    @model_validator(mode="after")
    def validate_unique_organizer_names(self) -> OrganizersRegistry:
        _unique_values([organizer.name.casefold() for organizer in self.organizers], "organizer names")
        return self


def validate_registry_relationships(
    sources: SourcesRegistry, organizers: OrganizersRegistry
) -> None:
    """Reject organizer profiles that cannot be tied to a configured source."""

    source_names = {source.name for source in sources.sources}
    unknown_profiles = {
        profile
        for organizer in organizers.organizers
        for profile in organizer.source_profiles
        if profile not in source_names
    }
    if unknown_profiles:
        rendered = ", ".join(sorted(unknown_profiles))
        raise ValueError(f"organizer source_profiles reference unknown sources: {rendered}")


class Settings(BaseSettings):
    """Secrets are only read from EventFinder's own environment/.env file."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="EVENTFINDER_",
        extra="ignore",
    )

    database_url: str = "sqlite:///./data/eventfinder.sqlite3"
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    gemini_api_key: str | None = None
    groq_api_key: str | None = None
    serper_api_key: str | None = None
    brave_search_api_key: str | None = None
    tavily_api_key: str | None = None
    exa_api_key: str | None = None
    config_dir: Path = PROJECT_ROOT / "config"

    @property
    def sqlalchemy_database_url(self) -> str:
        return self.database_url


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {}


@lru_cache
def get_settings() -> Settings:
    return Settings()


@lru_cache
def get_file_config() -> FileConfig:
    return FileConfig.model_validate(_read_yaml(get_settings().config_dir / "config.yaml"))


@lru_cache
def get_sources_registry() -> SourcesRegistry:
    return SourcesRegistry.model_validate(_read_yaml(get_settings().config_dir / "sources.yaml"))


@lru_cache
def get_organizers_registry() -> OrganizersRegistry:
    organizers = OrganizersRegistry.model_validate(
        _read_yaml(get_settings().config_dir / "organizers.yaml")
    )
    validate_registry_relationships(get_sources_registry(), organizers)
    return organizers
