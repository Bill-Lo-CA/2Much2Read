from __future__ import annotations

import re
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GmailFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    criteria: dict[str, str | bool | int]

    @field_validator("criteria")
    @classmethod
    def valid_criteria(cls, value: dict[str, str | bool | int]) -> dict[str, str | bool | int]:
        allowed = {
            "from",
            "to",
            "subject",
            "query",
            "negatedQuery",
            "hasAttachment",
            "excludeChats",
            "size",
            "sizeComparison",
        }
        if not value:
            raise ValueError("gmail filter criteria must not be empty")
        unknown = sorted(value.keys() - allowed)
        if unknown:
            raise ValueError(f"unsupported gmail filter criteria: {', '.join(unknown)}")
        return value


class Source(BaseModel):
    id: str
    name: str
    enabled: bool = True
    category: str = "OTHER"
    gmail_query: str
    gmail_filter: GmailFilter | None = None
    max_items_per_email: int = Field(default=10, ge=1, le=50)

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value):
            raise ValueError("source id must be a lowercase slug")
        if value == "list":
            raise ValueError("source id 'list' is reserved for the CLI; choose another id")
        return value


class Sources(BaseModel):
    sources: list[Source]

    @model_validator(mode="after")
    def unique_ids(self) -> Sources:
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source ids must be unique")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"
    gmail_credentials_path: Path = Path("google-client-secret.json")
    gmail_token_path: Path = Path("google-token.json")
    gmail_max_messages_per_run: int = Field(default=50, ge=1)
    gmail_lookback_days: int = Field(default=7, ge=1, le=30)
    gmail_oauth_callback_port: int = Field(default=8765, ge=1024, le=65535)
    sources_config_path: Path = Path("config/sources.yaml")
    database_path: Path = Path("newsletter-digest.sqlite3")
    lock_path: Path = Path("newsletter-digest.lock")
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:8b"
    ollama_num_ctx: int = 16384
    ollama_timeout_seconds: float = 300
    ollama_keep_alive: str = "10m"
    discord_webhook_url: str = ""
    discord_username: str = "Newsletter Digest"
    digest_language: str = "zh-TW"
    digest_timezone: str = "America/Montreal"
    digest_max_items: int = Field(default=10, ge=1)
    digest_top_items: int = Field(default=5, ge=0)


def load_sources(path: Path) -> Sources:
    if not path.is_file():
        raise ValueError(f"sources configuration not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Sources.model_validate(data)
