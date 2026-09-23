"""Typed, project-local configuration and source registries."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
    name: str
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
    allowed_domains: list[str] = []


class SourcesRegistry(BaseModel):
    sources: list[SourceDefinition]


class OrganizerDefinition(BaseModel):
    name: str
    aliases: list[str] = []
    domains: list[str] = []
    trust: Literal["high", "medium", "low"] = "low"
    online_allowed: bool = False
    source_profiles: list[str] = []


class OrganizersRegistry(BaseModel):
    organizers: list[OrganizerDefinition]


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
    return OrganizersRegistry.model_validate(_read_yaml(get_settings().config_dir / "organizers.yaml"))
