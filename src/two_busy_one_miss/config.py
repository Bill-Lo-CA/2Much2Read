from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _config_dir() -> Path:
    return Path.home() / ".config" / "2busy1miss"


def _data_dir() -> Path:
    return Path.home() / ".local" / "share" / "2busy1miss"


def settings_env_file() -> Path:
    return _config_dir() / "2busy1miss.env"


class CalendarConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    name: str | None = None
    enabled: bool = True


class ReminderSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    before: str
    id: str | None = None

    @field_validator("before")
    @classmethod
    def valid_offset(cls, value: str) -> str:
        if not re.fullmatch(r"[1-9]\d*[mhd]", value):
            raise ValueError("reminder offset must look like 5m, 2h, or 1d")
        return value


class EventMatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title_contains: list[str] = Field(default_factory=list)
    location_contains: list[str] = Field(default_factory=list)
    calendar_id: str | None = None
    has_location: bool | None = None
    all_day: bool | None = None


class RuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    match: EventMatch = Field(default_factory=EventMatch)
    reminders: list[ReminderSpec] = Field(min_length=1)


class RemindersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str | None = None
    calendars: list[CalendarConfig] = Field(min_length=1)
    default_rules: list[ReminderSpec] = Field(default_factory=list)
    rules: list[RuleConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        calendar_ids = [calendar.id for calendar in self.calendars]
        if len(calendar_ids) != len(set(calendar_ids)):
            raise ValueError("calendar ids must be unique")
        rule_ids = [rule.id for rule in self.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise ValueError("rule ids must be unique")
        return self

    @property
    def enabled_calendars(self) -> list[CalendarConfig]:
        return [calendar for calendar in self.calendars if calendar.enabled]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    google_calendar_credentials_path: Path = Field(default_factory=lambda: _config_dir() / "google-client-secret.json")
    google_calendar_token_path: Path = Field(default_factory=lambda: _config_dir() / "google-calendar-token.json")
    google_calendar_oauth_callback_port: int = Field(default=8765, ge=1024, le=65535)
    reminders_config_path: Path = Field(default_factory=lambda: _config_dir() / "reminders.yaml")
    database_path: Path = Field(default_factory=lambda: _data_dir() / "2busy1miss.sqlite3")
    lock_path: Path = Field(default_factory=lambda: _data_dir() / "2busy1miss.lock")
    discord_webhook_url: str = ""
    discord_username: str = "2busy1miss"
    reminder_timezone: str = "America/Montreal"
    reminder_lookahead_days: int = Field(default=7, ge=1, le=30)

    def __init__(self, **data: Any) -> None:
        super().__init__(_env_file=settings_env_file(), **data)


def load_reminders(path: Path) -> RemindersConfig:
    if not path.is_file():
        raise ValueError(f"reminders configuration not found: {path}")
    return RemindersConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
