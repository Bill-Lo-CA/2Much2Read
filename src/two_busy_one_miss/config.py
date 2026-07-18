from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from two_read_runtime.paths import config_dir, data_dir, env_file

MAX_REMINDER_OFFSET = timedelta(days=366)


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown IANA timezone {value!r}") from error
    return value


def settings_env_file() -> Path:
    return env_file("2busy1miss")


def reminder_data_dir() -> Path:
    target = data_dir()
    legacy = Path.home() / ".local" / "share" / "2busy1miss"
    if (legacy / "2busy1miss.sqlite3").is_file() and not (target / "2busy1miss.sqlite3").exists():
        return legacy
    return target


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
        match = re.fullmatch(r"([1-9]\d*)([mhd])", value)
        if match is None:
            raise ValueError("reminder offset must look like 5m, 2h, or 1d")
        amount, unit = int(match.group(1)), match.group(2)
        offset = timedelta(minutes=amount) if unit == "m" else timedelta(hours=amount) if unit == "h" else timedelta(days=amount)
        if offset > MAX_REMINDER_OFFSET:
            raise ValueError("reminder offset must not exceed 366d")
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
        if self.timezone is not None:
            _timezone(self.timezone)
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

    google_calendar_credentials_path: Path = Field(default_factory=lambda: config_dir() / "calendar-client-secret.json")
    google_calendar_token_path: Path = Field(default_factory=lambda: config_dir() / "calendar-token.json")
    google_calendar_oauth_callback_port: int = Field(default=8765, ge=1024, le=65535)
    reminders_config_path: Path = Field(default_factory=lambda: config_dir() / "reminders.yaml")
    database_path: Path = Field(default_factory=lambda: reminder_data_dir() / "2busy1miss.sqlite3")
    lock_path: Path = Field(default_factory=lambda: reminder_data_dir() / "2busy1miss.lock")
    discord_webhook_url: str = ""
    discord_username: str = "2busy1miss"
    reminder_timezone: str = "America/Montreal"
    reminder_lookahead_days: int = Field(default=7, ge=1, le=366)

    @field_validator("reminder_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        return _timezone(value)

    def __init__(self, **data: Any) -> None:
        super().__init__(_env_file=settings_env_file(), **data)


def load_reminders(path: Path) -> RemindersConfig:
    if not path.is_file():
        raise ValueError(f"reminders configuration not found: {path}")
    return RemindersConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
