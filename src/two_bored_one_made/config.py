from __future__ import annotations

import re
from datetime import time
from pathlib import Path
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from two_read_runtime.discord import DiscordDestination, configured_destination, configured_destinations
from two_read_runtime.endpoint_policy import EndpointPolicyError, validate_discord_webhook
from two_read_runtime.paths import app_data_file, config_dir, env_file

NUDGE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
# Discord's own limit is 2000 characters. The rest of the budget belongs to the mention prefix the
# transport prepends, so a message that validates here can never be split by length alone.
MAX_MESSAGE_CHARACTERS = 1800
MAX_DAILY_TIMES = 24
MAX_TOTAL_SENDS = 10_000


def _timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise ValueError(f"unknown IANA timezone {value!r}") from error
    return value


def settings_env_file() -> Path:
    return env_file("2bored1made")


class NudgeConfig(BaseModel):
    """One recurring message: what to say, when to say it, and how many times in total."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARACTERS)
    at: list[time] = Field(min_length=1, max_length=MAX_DAILY_TIMES)
    total_sends: int = Field(ge=1, le=MAX_TOTAL_SENDS)
    user_id: str | None = None
    webhook_url: str | None = None
    enabled: bool = True

    @field_validator("id")
    @classmethod
    def valid_id(cls, value: str) -> str:
        if not NUDGE_ID.fullmatch(value):
            raise ValueError("nudge id must contain only letters, digits, dot, dash, or underscore")
        return value

    @field_validator("message")
    @classmethod
    def message_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("nudge message must not be blank")
        return value

    @field_validator("at")
    @classmethod
    def valid_times(cls, value: list[time]) -> list[time]:
        if any(moment.second or moment.microsecond or moment.tzinfo for moment in value):
            raise ValueError("nudge times must use plain HH:MM")
        if len(set(value)) != len(value):
            raise ValueError("nudge times must be unique")
        return sorted(value)

    @field_validator("user_id")
    @classmethod
    def valid_user_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not (value.isascii() and value.isdecimal()):
            raise ValueError("nudge user_id must be a numeric Discord user ID")
        return value

    @field_validator("webhook_url")
    @classmethod
    def valid_webhook_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return validate_discord_webhook(value)
        except EndpointPolicyError as error:
            raise ValueError(f"nudge webhook_url is not a usable Discord webhook: {error.code}") from None


class NudgesConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str | None = None
    nudges: list[NudgeConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if self.timezone is not None:
            _timezone(self.timezone)
        ids = [nudge.id for nudge in self.nudges]
        if len(ids) != len(set(ids)):
            raise ValueError("nudge ids must be unique")
        return self

    @property
    def enabled_nudges(self) -> list[NudgeConfig]:
        return [nudge for nudge in self.nudges if nudge.enabled]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    discord_delivery_mode: Literal["webhook", "bot", "both"] = "webhook"
    discord_webhook_url: str = ""
    discord_username: str = "2bored1made"
    discord_bot_token: str = ""
    discord_bot_channel_id: str = ""
    discord_allowed_mention_ids: str = ""
    nudges_config_path: Path = Field(default_factory=lambda: config_dir() / "nudges.yaml")
    database_path: Path = Field(default_factory=lambda: app_data_file("2bored1made", "2bored1made.sqlite3"))
    lock_path: Path = Field(default_factory=lambda: app_data_file("2bored1made", "2bored1made.lock"))
    nudge_timezone: str = "America/Montreal"

    @field_validator("discord_allowed_mention_ids")
    @classmethod
    def valid_allowed_mention_ids(cls, value: str) -> str:
        if not value.strip():
            return ""
        user_ids = [user_id.strip() for user_id in value.split(",")]
        if not all(user_id.isascii() and user_id.isdecimal() for user_id in user_ids):
            raise ValueError("DISCORD_ALLOWED_MENTION_IDS must be comma-separated numeric Discord user IDs")
        return ",".join(user_ids)

    @field_validator("nudge_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        return _timezone(value)

    @property
    def allowed_mention_ids(self) -> set[str]:
        return set(self.discord_allowed_mention_ids.split(",")) if self.discord_allowed_mention_ids else set()

    def __init__(self, **data: Any) -> None:
        super().__init__(_env_file=settings_env_file(), **data)

    def discord_destinations(self) -> list[DiscordDestination]:
        return configured_destinations(
            self.discord_delivery_mode, self.discord_webhook_url, self.discord_bot_token, self.discord_bot_channel_id
        )

    def discord_destination(self) -> DiscordDestination:
        return configured_destination(
            self.discord_delivery_mode, self.discord_webhook_url, self.discord_bot_token, self.discord_bot_channel_id
        )


def load_nudges(path: Path) -> NudgesConfig:
    if not path.is_file():
        raise ValueError(f"nudges configuration not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ValueError(f"invalid nudges configuration: {path}") from error
    return NudgesConfig.model_validate(data or {})
